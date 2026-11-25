"""Tests for auth security primitives — rate limiting, constant-time compare, sessions.

Security-critical paths in api/auth.py:
- _rate_limit_check: sliding-window limiter that throttles login/pairing brute force.
- hash_secret / verify_secret: SHA-256 hashing + constant-time (hmac) comparison.
- session create/validate/destroy: constant-time token lookup with expiry.

The limiter and session store are module-level globals, so each test resets
them and drives a controllable fake clock to stay deterministic.
"""

import hashlib

import pytest

import api.auth as auth
from api.auth import (
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    hash_secret,
    verify_secret,
    _rate_limit_check,
    _create_session,
    _validate_session,
    _destroy_session,
)


class FakeClock:
    """A monotonic-ish clock we advance manually."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replace auth's `time` module reference with a controllable fake."""
    c = FakeClock()
    monkeypatch.setattr(auth, "time", c)
    yield c


@pytest.fixture
def clean_limiter(monkeypatch):
    """Reset the global rate-limit state before/after each test."""
    auth._rate_limits.clear()
    auth._login_failures.clear()
    monkeypatch.setattr(auth, "_rate_limit_last_cleanup", 0.0)
    yield
    auth._rate_limits.clear()
    auth._login_failures.clear()


@pytest.fixture
def clean_sessions():
    """Reset the global session store before/after each test."""
    auth._sessions.clear()
    yield
    auth._sessions.clear()


class TestRateLimit:
    """_rate_limit_check sliding-window behaviour."""

    def test_allows_up_to_max(self, clock, clean_limiter):
        for i in range(RATE_LIMIT_MAX):
            assert _rate_limit_check("login:1.2.3.4") is True, f"attempt {i} blocked"

    def test_blocks_after_max(self, clock, clean_limiter):
        for _ in range(RATE_LIMIT_MAX):
            _rate_limit_check("login:1.2.3.4")
        # The (MAX+1)-th attempt within the window is blocked.
        assert _rate_limit_check("login:1.2.3.4") is False

    def test_keys_are_independent(self, clock, clean_limiter):
        for _ in range(RATE_LIMIT_MAX):
            _rate_limit_check("login:1.1.1.1")
        assert _rate_limit_check("login:1.1.1.1") is False
        # A different IP starts with a clean budget.
        assert _rate_limit_check("login:2.2.2.2") is True

    def test_window_expiry_resets_budget(self, clock, clean_limiter):
        for _ in range(RATE_LIMIT_MAX):
            _rate_limit_check("login:9.9.9.9")
        assert _rate_limit_check("login:9.9.9.9") is False

        # Advance past the window: old timestamps are pruned, budget restored.
        clock.advance(RATE_LIMIT_WINDOW + 1)
        assert _rate_limit_check("login:9.9.9.9") is True

    def test_partial_window_does_not_reset(self, clock, clean_limiter):
        for _ in range(RATE_LIMIT_MAX):
            _rate_limit_check("login:5.5.5.5")
        # Still inside the window -> still blocked.
        clock.advance(RATE_LIMIT_WINDOW - 1)
        assert _rate_limit_check("login:5.5.5.5") is False

    def test_lru_eviction_bounds_memory(self, clock, clean_limiter, monkeypatch):
        """The limiter evicts oldest keys past the max-keys cap."""
        monkeypatch.setattr(auth, "_RATE_LIMIT_MAX_KEYS", 3)
        for i in range(10):
            _rate_limit_check(f"login:ip-{i}")
        assert len(auth._rate_limits) <= 3


class TestHashingAndConstantTimeCompare:
    """hash_secret / verify_secret."""

    def test_hash_is_sha256_hex(self):
        h = hash_secret("my-token")
        assert h == hashlib.sha256(b"my-token").hexdigest()
        assert len(h) == 64

    def test_hash_is_deterministic(self):
        assert hash_secret("abc") == hash_secret("abc")

    def test_different_inputs_differ(self):
        assert hash_secret("abc") != hash_secret("abd")

    def test_verify_correct_secret(self):
        stored = hash_secret("correct-horse")
        assert verify_secret("correct-horse", stored) is True

    def test_verify_wrong_secret(self):
        stored = hash_secret("correct-horse")
        assert verify_secret("wrong-horse", stored) is False

    def test_verify_against_garbage_hash(self):
        assert verify_secret("anything", "not-a-real-hash") is False

    def test_verify_is_case_sensitive(self):
        stored = hash_secret("Secret")
        assert verify_secret("secret", stored) is False


@pytest.mark.usefixtures("direct_admin_on")
class TestSessions:
    """Session create / validate / destroy (constant-time token lookup)."""

    def test_create_and_validate(self, clock, clean_sessions):
        token = _create_session("alice", "agent-x")
        session = _validate_session(token, "agent-x")
        assert session is not None
        assert session["username"] == "alice"

    def test_validate_unknown_token(self, clock, clean_sessions):
        _create_session("alice", "agent-x")
        assert _validate_session("0" * 64, "agent-x") is None

    def test_validate_expired_session(self, clock, clean_sessions):
        token = _create_session("member_two", "agent-x")
        clock.advance(auth.SESSION_LIFETIME + 1)
        assert _validate_session(token, "agent-x") is None

    def test_user_agent_mismatch_rejected(self, clock, clean_sessions):
        """Session bound to a UA must not validate under a different UA."""
        token = _create_session("carol", "browser-A")
        assert _validate_session(token, "browser-B") is None
        # Same UA still works.
        assert _validate_session(token, "browser-A") is not None

    def test_destroy_session(self, clock, clean_sessions):
        token = _create_session("dave", "agent-x")
        _destroy_session(token)
        assert _validate_session(token, "agent-x") is None

    def test_per_user_session_limit(self, clock, clean_sessions):
        """Old sessions are evicted past MAX_SESSIONS_PER_USER."""
        tokens = []
        for _ in range(auth.MAX_SESSIONS_PER_USER + 5):
            clock.advance(1)  # distinct created_at for deterministic eviction
            tokens.append(_create_session("eve", "agent-x"))
        active = [t for t in tokens if _validate_session(t, "agent-x") is not None]
        assert len(active) <= auth.MAX_SESSIONS_PER_USER
        # The most recently created session must still be valid.
        assert _validate_session(tokens[-1], "agent-x") is not None
