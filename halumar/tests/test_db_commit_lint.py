"""The lint script finds transaction control outside database.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_db_commits  # noqa: E402


def test_lint_finds_commit_rollback_connection_and_tx_sql(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "database.py").write_text("await self.commit()\n")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "x.py").write_text(
        "async def f(db, name):\n"
        "    await db.commit()\n"
        "    await db.connection.rollback()\n"
        '    await db.execute(f"SAVEPOINT {name}")\n'
        '    await db.execute("SELECT 1")\n'
        "    async with db.transaction():\n"
        "        pass\n"
    )
    found = check_db_commits.scan(str(tmp_path))
    kinds = sorted((line, kind) for _, line, kind, _ in found)
    assert kinds == [
        (2, "commit()"),
        (3, ".connection"),
        (3, "rollback()"),
        (4, "sql:SAVEPOINT"),
    ]
    assert check_db_commits.main([str(tmp_path)]) == 0
    assert check_db_commits.main(["--strict", str(tmp_path)]) == 1


def test_allowlist_limits_counts_and_must_shrink(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "legacy.py").write_text(
        "async def f(db):\n    await db.commit()\n    await db.commit()\n"
    )
    allow = tmp_path / "allow.txt"
    root = str(tmp_path)

    allow.write_text("# comment\napi/legacy.py 2  # TODO move to transaction()\n")
    assert check_db_commits.main(["--strict", "--allowlist", str(allow), root]) == 0

    # a third call site in an allowlisted file fails
    (tmp_path / "api" / "legacy.py").write_text(
        "async def f(db):\n" + "    await db.commit()\n" * 3
    )
    assert check_db_commits.main(["--strict", "--allowlist", str(allow), root]) == 1

    # a converted call site needs the entry lowered (the list only shrinks)
    (tmp_path / "api" / "legacy.py").write_text(
        "async def f(db):\n    await db.commit()\n"
    )
    assert check_db_commits.main(["--strict", "--allowlist", str(allow), root]) == 1
    allow.write_text("api/legacy.py 1\n")
    assert check_db_commits.main(["--strict", "--allowlist", str(allow), root]) == 0

    # files not on the list never pass
    (tmp_path / "api" / "new.py").write_text(
        "async def g(db):\n    await db.commit()\n"
    )
    assert check_db_commits.main(["--strict", "--allowlist", str(allow), root]) == 1
