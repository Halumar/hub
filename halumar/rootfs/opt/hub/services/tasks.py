"""Background task registry: every task has an owner and ends on shutdown.

``asyncio.create_task`` without a kept reference can be garbage-collected
mid-flight, and an exception in a long-running loop would end it silently.
All background work of the hub goes through the registry instead:

- ``spawn(coro, name)`` for one-shot work (keeps a reference, logs failures);
- ``supervise(name, factory)`` for loops: a crash is logged and the loop is
  restarted with backoff, a normal return ends it;
- ``shutdown(timeout)`` cancels everything and waits at most ``timeout``.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("hub.services.tasks")

# Restart delay of a crashed supervised loop: doubles per crash in a row,
# capped, with jitter. A loop that ran longer than STABLE_SECONDS starts over.
RESTART_INITIAL_SECONDS = 1.0
RESTART_MAX_SECONDS = 60.0
STABLE_SECONDS = 60.0


def backoff_delay(attempt: int, initial: float, cap: float) -> float:
    """Exponential backoff with jitter: a random value in [base/2, base].

    ``base = min(cap, initial * 2**attempt)``. The lower half keeps reconnects
    from hammering, the random upper half spreads many clients apart.
    """
    base = min(cap, initial * (2 ** max(0, attempt)))
    return base / 2 + random.uniform(0, base / 2)


class TaskRegistry:
    """Owns the hub's background tasks (one instance per process)."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def names(self) -> list[str]:
        """Names of the running tasks (diagnostics, tests)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        return sorted(
            t.get_name()
            for t in self._tasks
            if not t.done() and (loop is None or t.get_loop() is loop)
        )

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background task %s failed: %s",
                task.get_name(),
                exc,
                exc_info=exc,
            )

    def spawn(self, coro: Awaitable[Any], name: str = "task") -> asyncio.Task:
        """Run ``coro`` in the background, keep a reference until it ends."""
        if self._closing:
            # Shutdown in progress: nothing new may start (and outlive it).
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("task registry is shutting down")
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        return self._track(task)

    def supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        restart_initial: float = RESTART_INITIAL_SECONDS,
        restart_max: float = RESTART_MAX_SECONDS,
    ) -> asyncio.Task:
        """Run the loop ``factory()`` and restart it after a crash."""

        async def _runner() -> None:
            crashes = 0
            loop = asyncio.get_running_loop()
            while True:
                started = loop.time()
                try:
                    await factory()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - restarted below
                    if loop.time() - started >= STABLE_SECONDS:
                        crashes = 0
                    delay = backoff_delay(crashes, restart_initial, restart_max)
                    crashes += 1
                    logger.error(
                        "Loop %s crashed (%s); restarting in %.1fs",
                        name,
                        e,
                        delay,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay)

        return self.spawn(_runner(), name=name)

    async def shutdown(self, timeout: float = 5.0) -> list[str]:
        """Cancel all tasks and wait up to ``timeout`` seconds.

        Returns the names of tasks that did not end in time (logged).
        """
        self._closing = True
        loop = asyncio.get_running_loop()
        tasks = [t for t in self._tasks if not t.done() and t.get_loop() is loop]
        for task in tasks:
            task.cancel()
        if not tasks:
            self._closing = False
            return []
        try:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
        finally:
            self._closing = False
        stuck = sorted(t.get_name() for t in pending)
        if stuck:
            logger.warning(
                "Background tasks did not stop within %.1fs: %s",
                timeout,
                ", ".join(stuck),
            )
        return stuck

    def reset(self) -> None:
        """Forget state after a shutdown (tests, a second lifespan run).

        Tasks of another (finished) event loop are dropped as well.
        """
        self._closing = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self._tasks = {t for t in self._tasks if not t.done() and t.get_loop() is loop}


registry = TaskRegistry()


def spawn(coro: Awaitable[Any], name: str = "task") -> asyncio.Task:
    """Module-level shortcut for ``registry.spawn``."""
    return registry.spawn(coro, name)
