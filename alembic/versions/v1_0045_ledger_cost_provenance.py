"""Record where each spend figure came from (#141 phase 1).

``cost_basis`` and ``price_source`` were already computed at exactly the point a
ledger row is written, then passed to a log line and dropped
(``graph/budget.py:514``). Its own comment claims "recording the basis is the
point of the whole exercise: a figure nobody can attribute is the state we
started in" — but nothing persisted it, so that state is where we still were.

This matters more here than it would elsewhere, because almost nothing tells us
what a call actually cost. Only OpenRouter returns ``usage.cost``
(``llm.py:194``); every other provider returns tokens, and the dollar figure is
our own arithmetic against our own rate card — while being authoritative for
enforcement. Without these two columns there is no way, after the fact, to tell
a measured dollar from an inferred one, or to know which rate card produced it.

Both nullable and backfilled to NULL rather than to a guess: rows written
before this shipped genuinely do not carry the information, and inventing
``estimated`` for them would be the same category of error the columns exist to
expose. NULL means "unknown provenance", which is the truth.

Revision ID: v1_0045_ledger_cost_provenance
Revises: v1_0044_llm_spend_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v1_0045_ledger_cost_provenance"
down_revision: str | None = "v1_0044_llm_spend_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "llm_spend_ledger",
        sa.Column("cost_basis", sa.Text(), nullable=True),
    )
    op.add_column(
        "llm_spend_ledger",
        sa.Column("price_source", sa.Text(), nullable=True),
    )
    # Reporting reads "how much of this tenant's spend is inferred", which is a
    # scan over a day's rows filtered by basis.
    op.create_index(
        "ix_llm_spend_ledger_cost_basis",
        "llm_spend_ledger",
        ["tenant_id", "cost_basis"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_spend_ledger_cost_basis", table_name="llm_spend_ledger")
    op.drop_column("llm_spend_ledger", "price_source")
    op.drop_column("llm_spend_ledger", "cost_basis")
