"""Two migration runners against one database must not corrupt it (#135).

Migrations run in the API Deployment's ``db-init`` initContainer, so every
replica starts one. ``values.schema.json`` puts no maximum on replicas, so a
fresh install at ``replicas: 2`` starts two runners simultaneously. Before the
advisory lock in ``alembic/env.py`` there was nothing serialising them.

This test exists because the first attempt at that lock did not actually fix
the race: it was taken *after* the ``CREATE TABLE IF NOT EXISTS
alembic_version`` block, and that statement is not atomic against a concurrent
create — the second runner died on a duplicate ``pg_type`` row before alembic
was even involved. Reading the diff did not reveal that; racing two real
runners did. So the regression guard has to race real runners too.

Marked ``integration``: it needs a Postgres it can create from empty.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest


SKIP_INTEGRATION = os.getenv("SKIP_INTEGRATION", "0") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        SKIP_INTEGRATION,
        reason="SKIP_INTEGRATION set; migration concurrency test needs Postgres",
    ),
]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _target_url() -> str:
    """An async DSN pointing at a database this test may migrate from empty."""
    return os.getenv(
        "DATABASE_URL_MIGRATION_RACE",
        os.getenv(
            "DATABASE_URL_ADMIN",
            "postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5432/soctalk",
        ),
    )


def _spawn_upgrade(url: str) -> subprocess.Popen:
    """Start `alembic upgrade head` as its own process.

    Separate processes, not threads: each initContainer is a process with its
    own engine and its own session, which is what the advisory lock has to
    serialise. Threads sharing a connection pool would not reproduce it.
    """
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.Popen(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_concurrent_migration_runners_do_not_corrupt_the_schema():
    url = _target_url()

    first = _spawn_upgrade(url)
    second = _spawn_upgrade(url)
    out_first = first.communicate()[0]
    out_second = second.communicate()[0]

    # Both must succeed. The loser blocking and then finding itself at head is
    # the intended outcome; a non-zero exit means the race is back.
    assert first.returncode == 0, f"first runner failed:\n{out_first}"
    assert second.returncode == 0, f"second runner failed:\n{out_second}"

    combined = out_first + out_second
    for smell in ("duplicate key", "already exists", "Traceback"):
        assert smell.lower() not in combined.lower(), (
            f"concurrent runners raced ({smell!r} in output):\n{combined}"
        )

    # Exactly one runner should have done the work. If both applied revisions,
    # they were not serialised and the schema was written twice.
    applied_first = out_first.count("Running upgrade")
    applied_second = out_second.count("Running upgrade")
    assert min(applied_first, applied_second) == 0, (
        "both runners applied revisions, so the lock did not serialise them: "
        f"{applied_first} and {applied_second}"
    )
