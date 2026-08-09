"""Give the primary tenant LLM config a serving engine (#142).

Pricing classifies a backend by ``provider_kind_for(provider, base_url,
engine)``, and it cannot tell a self-hosted served endpoint from a generic
OpenAI-compatible gateway by host alone. Per-tier configs already carried an
``engine``; the primary config had no equivalent field.

The consequence was narrow but exact: an install running its own vLLM/SGLang
set ``defaults.llm.engine``, so the API pod priced fleet chat as
``self_hosted`` — but a tenant onboarded from those install defaults stored
provider/base_url/model with no engine, so its own price gate classified the
same backend as ``openai_compatible`` and missed the ``self_hosted`` catalog
entry. The model was called and never priced, which is the failure #142 exists
to close (Codex review of #142, round 13).

NULL means "a hosted vendor or a generic gateway", which is what every
existing row is, so this is additive and needs no backfill.

Revision ID: v1_0047_tenant_llm_engine
Revises: v1_0046_chat_unpriced_spend
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v1_0047_tenant_llm_engine"
down_revision = "v1_0046_chat_unpriced_spend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_configs",
        sa.Column("llm_engine", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_configs", "llm_engine")
