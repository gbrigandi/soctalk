"""Add workflow resumption marker to pending_reviews.

Revision ID: add_workflow_resumed_at
Revises: remove_settings_secrets
Create Date: 2026-01-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_workflow_resumed_at"
down_revision: str | None = "remove_settings_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(connection: sa.Connection, table_name: str, column_name: str) -> bool:
    result = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    connection = op.get_bind()
    if not _column_exists(connection, "pending_reviews", "workflow_resumed_at"):
        op.add_column(
            "pending_reviews",
            sa.Column("workflow_resumed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    connection = op.get_bind()
    if _column_exists(connection, "pending_reviews", "workflow_resumed_at"):
        op.drop_column("pending_reviews", "workflow_resumed_at")

