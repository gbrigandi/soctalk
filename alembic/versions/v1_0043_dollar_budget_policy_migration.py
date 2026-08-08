"""Migrate per-tenant dollar budget to a policy override (#128).

The dollar twin of v1_0040. The per-run dollar budget moved off the
env-rendered TenantIntegration field ``llm_dollar_budget_per_run`` (which
needed a worker rollout to change, and never reached the run row, so the row
showed a budget that was not the one being enforced) onto a DB-resolved policy
override on ``max_dollars_per_investigation``, resolved and stamped at run
creation.

This copy is a PREREQUISITE for inverting the worker's env-vs-claim precedence.
Flipping first would silently RAISE any tenant holding a low env value, because
the run row still carries the 5.0 column default: an install capping a tenant at
$0.50 through env would jump to $5.00 the moment the claim payload started
winning (Codex review, finding 1).

Forward-only: the source column is left intact, so downgrade loses no data.
``ON CONFLICT DO NOTHING`` never clobbers an override already set through the
run-budget resource.

Revision ID: v1_0043_dollar_budget_policy_migration
Revises: v1_0042_model_price_catalog
"""

from __future__ import annotations

from alembic import op

revision = "v1_0043_dollar_budget_policy_migration"
down_revision: str | None = "v1_0042_model_price_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tenant_policies (tenant_id, key, value, updated_at)
        SELECT tenant_id,
               'max_dollars_per_investigation',
               to_jsonb(llm_dollar_budget_per_run),
               now()
        FROM integration_configs
        WHERE llm_dollar_budget_per_run IS NOT NULL
        ON CONFLICT (tenant_id, key) DO NOTHING;
        """
    )
    # Backfill the RUNS too, not just the policy. Copying the policy alone is
    # not enough to make the precedence flip safe: every run row that already
    # exists carries the 5.0 column default, because nothing stamped
    # dollars_budget before this release. The moment the worker starts
    # preferring the claim, those rows would raise a tenant that was capped
    # lower -- exactly the silent-raise this migration exists to prevent
    # (Codex review, finding 1).
    #
    # Only runs that can still spend are touched. A completed or failed run's
    # budget is history and rewriting it would falsify what it actually ran
    # under.
    op.execute(
        """
        UPDATE investigation_runs r
           SET dollars_budget = c.llm_dollar_budget_per_run
          FROM integration_configs c
         WHERE c.tenant_id = r.tenant_id
           AND c.llm_dollar_budget_per_run IS NOT NULL
           AND r.status IN ('active', 'paused', 'waiting_on_gate', 'halted_budget');
        """
    )


def downgrade() -> None:
    # Forward-only data copy, mirroring v1_0040: the source column is untouched
    # so the values still live in integration_configs, and a blanket delete of
    # the key would also wipe overrides set directly through the run-budget
    # resource.
    pass
