"""Time-bucketed spend ledger, so a daily ceiling means the day it was spent (#129).

Daily spend was summed off ``investigation_runs`` and bucketed by
``COALESCE(ended_at, lease_expires_at, claimed_at, started_at)`` — a run
LIFECYCLE timestamp, not when the money was spent. That was merely approximate
under a rolling 24h window. Under a calendar day it is wrong in a way that
defeats the point of the reset:

  * a run that spends $49 before midnight and completes after it charges the
    whole $49 to the new day;
  * unlocking a halted run after midnight moves yesterday's spend into today,
    because unlock clears the lifecycle timestamps and the next claim restamps
    them.

So spend gets its own rows, stamped when it is reported. The API already
receives a CUMULATIVE ``dollars_used``/``tokens_used`` on every heartbeat and
on completion, so the delta between reports is recorded here with the time it
arrived — no worker change, and granularity as fine as the heartbeat interval.

``chat_messages`` already carries ``created_at`` and was always bucketed
correctly; it stays where it is.

Revision ID: v1_0044_llm_spend_ledger
Revises: v1_0043_dollar_budget_policy_migration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v1_0044_llm_spend_ledger"
down_revision: str | None = "v1_0043_dollar_budget_policy_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_spend_ledger",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        # When the spend was REPORTED. This is the column the daily window
        # scopes on, and nothing in the run lifecycle can move it afterwards.
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("tokens_delta", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("dollars_delta", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["investigation_runs.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_llm_spend_ledger_tenant_time",
        "llm_spend_ledger",
        ["tenant_id", "occurred_at"],
    )
    op.execute("ALTER TABLE llm_spend_ledger ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_spend_ledger FORCE ROW LEVEL SECURITY")
    # Same shape as investigation_runs_tenant_isolation: fleet-scope MSSP reads
    # across tenants, everyone else is pinned to their own.
    op.execute(
        """
        CREATE POLICY llm_spend_ledger_tenant_isolation ON llm_spend_ledger
        USING (
            (COALESCE(NULLIF(current_setting('app.current_tenant_id', true), ''), '') = ''
             AND current_setting('app.current_audience', true) = 'mssp')
            OR NOT (tenant_id IS DISTINCT FROM
                    NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
        )
        """
    )
    # Seed from what history we have, so the ceiling is not wrong on upgrade
    # day. The lifecycle timestamp is the only time these rows carry; it is the
    # same approximation as before, applied once, and every FUTURE report is
    # stamped properly.
    op.execute(
        """
        INSERT INTO llm_spend_ledger (tenant_id, run_id, occurred_at, tokens_delta, dollars_delta)
        SELECT tenant_id, id,
               COALESCE(ended_at, lease_expires_at, claimed_at, started_at),
               tokens_used, dollars_used
          FROM investigation_runs
         WHERE COALESCE(tokens_used, 0) > 0 OR COALESCE(dollars_used, 0) > 0
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS llm_spend_ledger_tenant_isolation ON llm_spend_ledger")
    op.drop_index("ix_llm_spend_ledger_tenant_time", table_name="llm_spend_ledger")
    op.drop_table("llm_spend_ledger")
