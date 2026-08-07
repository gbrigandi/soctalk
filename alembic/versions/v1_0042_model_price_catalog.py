"""Install-level model price catalog + per-run price snapshot (#125).

Two tables' worth of change, one purpose: make what a model costs a fact the
platform knows rather than a number nobody maintains.

``model_prices`` is deliberately GLOBAL. Nearly every table in this schema
carries ``tenant_id`` and runs under tenant context; this one does not, because
a price is a property of the market, not of a customer. It carries no tenant
column and no RLS policy, and writes are platform-admin only.

``investigation_runs.price_snapshot`` is the other half. Prices resolve when a
run is created and the rates ride on the run, mirroring how #103 stamps
``tokens_budget``: the worker prices from the run rather than from its
environment, a price correction never rewrites an in-flight run or history, and
changing a price needs no rollout.

The snapshot is role-keyed (fast / reasoning), not model-keyed: a tenant can
point both roles at the same model string through different providers, and
those cost different amounts.

Nullable, and no data is seeded here: a schema migration should not smuggle in
mutable market data. A run created by an older API stamps NULL and prices the
legacy way, which is what keeps a rolling upgrade boring.

Revision ID: v1_0042_model_price_catalog
Revises: v1_0041_llm_model_prices
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "v1_0042_model_price_catalog"
down_revision: str | None = "v1_0041_llm_model_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # 'anthropic' | 'openai' | 'openrouter' | 'openai_compatible' |
        # 'self_hosted'. Deliberately the PROTOCOL/vendor kind, because
        # "openai-compatible" alone identifies nothing: one gateway can route
        # the same model string to different upstreams at different prices.
        sa.Column("provider_kind", sa.Text(), nullable=False),
        # Vendor slug when we know it ('deepseek', 'moonshot'); NULL for a
        # generic compatible endpoint we cannot attribute.
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        # Rates in integer micro-dollars per million tokens, in JSONB rather
        # than columns. Integers because float dollars do not survive
        # arithmetic honestly; JSONB because pricing has stopped being two
        # numbers — cache reads and writes today, reasoning and image and audio
        # tokens and batch multipliers next — and a new dimension should not be
        # a migration. Keys the application understands are validated there.
        sa.Column(
            "dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        # 'curated' | 'operator' | 'provider_declared' | 'imported'. Every price
        # says where it came from, so the effective rate is auditable and the
        # unknown case stops being invisible.
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("as_of", sa.Date(), nullable=True),
        sa.Column("license_status", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # provider_id is nullable, and NULL never equals NULL in a UNIQUE index, so
    # a plain three-column constraint would let unlimited duplicate rows exist
    # for a generic compatible endpoint. Two partial indexes cover both cases.
    op.create_index(
        "ix_model_prices_key_with_provider",
        "model_prices",
        ["provider_kind", "provider_id", "model"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )
    op.create_index(
        "ix_model_prices_key_no_provider",
        "model_prices",
        ["provider_kind", "model"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NULL"),
    )

    op.add_column(
        "investigation_runs",
        sa.Column(
            "price_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("investigation_runs", "price_snapshot")
    op.drop_index("ix_model_prices_key_no_provider", table_name="model_prices")
    op.drop_index("ix_model_prices_key_with_provider", table_name="model_prices")
    op.drop_table("model_prices")
