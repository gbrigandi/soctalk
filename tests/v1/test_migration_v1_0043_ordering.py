"""v1_0043 must backfill runs BEFORE it writes the policy (#128).

Caught by running a real upgrade rather than reasoning about it. The backfill
is guarded by "no pre-existing tenant_policies override", and the policy INSERT
in the same migration writes exactly such a row — so with the insert first, the
guard saw the row the migration had just created, the backfill never fired, and
every in-flight run stayed on the 5.0 column default. That is the silent raise
the migration exists to prevent: a tenant capped at $0.50 through the legacy
column would have run at $5 the moment the worker started preferring the row.
"""
from __future__ import annotations

import pathlib
import re


def _upgrade_body() -> str:
    src = pathlib.Path(
        "alembic/versions/v1_0043_dollar_budget_policy_migration.py"
    ).read_text()
    return src[src.index("def upgrade()") : src.index("def downgrade()")]


def test_run_backfill_precedes_the_policy_insert():
    body = _upgrade_body()
    backfill = body.index("UPDATE investigation_runs")
    insert = body.index("INSERT INTO tenant_policies")
    assert backfill < insert, (
        "the run backfill must run before the policy insert, or its "
        "NOT EXISTS(policy) guard matches the row this migration just wrote"
    )


def test_backfill_only_touches_default_budgets_without_an_override():
    """Both halves of the guard must survive any future edit."""
    body = _upgrade_body()
    update = body[body.index("UPDATE investigation_runs") :]
    update = update[: update.index('"""')]
    assert "dollars_budget = 5.0" in update, "lost the column-default guard"
    assert "NOT EXISTS" in update, "lost the pre-existing-override guard"
    # Terminal runs are history and must not be rewritten.
    assert "'completed'" not in update and "'failed'" not in update
    for status in ("active", "paused", "waiting_on_gate", "halted_budget"):
        assert f"'{status}'" in update, status
