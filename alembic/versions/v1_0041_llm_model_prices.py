"""Per-tenant model price overlay (#121).

#5 made pricing configurable through ``SOCTALK_MODEL_PRICES``, but nothing ever
set it for a provisioned tenant: an MSSP admin could point a tenant at any
OpenAI-compatible model through the config API and had no way to say what that
model costs. Every model outside the built-in table then billed at the
fail-expensive $15/$75 fallback, which over-reports spend, halts runs on the
per-run dollar budget, and trips the tenant daily cap that gates run claiming.

This column is the missing input: a price overlay the render path emits into the
runs-worker env. NULL keeps today's behaviour exactly.

Revision ID: v1_0041_llm_model_prices
Revises: v1_0040_run_budget_policy_migration
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v1_0041_llm_model_prices"
down_revision: str | None = "v1_0040_run_budget_policy_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_configs",
        sa.Column(
            "llm_model_prices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("integration_configs", "llm_model_prices")
