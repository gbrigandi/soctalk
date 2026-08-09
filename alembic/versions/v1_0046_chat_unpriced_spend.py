"""Track unpriced chat spend so it stops enforcing (#124, #141 phase 2).

Phase 2 exempted unpriced spend from the worker's budgets, but chat was only
half fixed: the between-iteration check used the enforceable figure while the
FULL amount still went to ``chat_messages.dollars`` and
``conversations.total_dollars``. Three consequences, all found by review:

* the next turn gates on the conversation total, so an unpriced turn still
  blocked later chat;
* the tenant daily ceiling sums ``chat_messages.dollars`` unconditionally, and
  that ceiling gates the worker's claim loop — so an unpriced chat session
  could stop triage;
* neither figure could be separated after the fact.

The recorded amount stays whole on both rows. These columns say how much of it
is a guess, so enforcement can subtract it while the bill still shows what was
spent.

Revision ID: v1_0046_chat_unpriced_spend
Revises: v1_0045_ledger_cost_provenance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "v1_0046_chat_unpriced_spend"
down_revision: str | None = "v1_0045_ledger_cost_provenance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL with a zero default: unlike the ledger's provenance columns,
    # "unknown" is not a meaningful state here. A pre-existing row was priced
    # by whatever rules applied then, and treating its spend as unpriced would
    # retroactively un-enforce history. Zero says "none of this was a guess",
    # which is the conservative reading and keeps existing ceilings intact.
    op.add_column(
        "chat_messages",
        sa.Column(
            "dollars_unpriced",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "dollars_unpriced",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "dollars_unpriced")
    op.drop_column("chat_messages", "dollars_unpriced")
