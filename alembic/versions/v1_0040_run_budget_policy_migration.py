"""Migrate per-tenant token budget to a policy override (#103).

The per-run token budget moved from the env-rendered TenantIntegration field
``llm_token_budget_per_run`` (which needed a worker rollout to change) to a
DB-resolved policy override on ``max_tokens_per_investigation``, resolved at run
creation with no rollout. Copy existing non-null per-tenant values into
``tenant_policies`` so no tenant loses its configured budget.

Forward-only: the source column is left intact (vestigial), so downgrade loses
no data. ``ON CONFLICT DO NOTHING`` never clobbers an override already set via
the new run-budget resource.

Revision ID: v1_0040_run_budget_policy_migration
Revises: v1_0039_run_transient_retry
"""

from __future__ import annotations

from alembic import op

revision = "v1_0040_run_budget_policy_migration"
down_revision: str | None = "v1_0039_run_transient_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO tenant_policies (tenant_id, key, value, updated_at)
        SELECT tenant_id,
               'max_tokens_per_investigation',
               to_jsonb(llm_token_budget_per_run),
               now()
        FROM integration_configs
        WHERE llm_token_budget_per_run IS NOT NULL
        ON CONFLICT (tenant_id, key) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Forward-only data copy. The source column was not modified, so the values
    # still live in integration_configs; leaving the migrated policy rows in
    # place loses nothing. A blanket delete of the key would also wipe overrides
    # set directly via the run-budget resource, so downgrade is a no-op.
    pass
