"""add missing investigation columns and pending_reviews table

Revision ID: add_missing_columns
Revises: 5da1648a4fc3
Create Date: 2025-12-09 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_missing_columns"
down_revision: str | None = "5da1648a4fc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add missing columns to investigations table
    # Using raw SQL with IF NOT EXISTS for idempotency
    connection = op.get_bind()

    # Check if suspicious_count column exists
    result = connection.execute(
        sa.text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='investigations' AND column_name='suspicious_count'
        """)
    )
    if not result.fetchone():
        op.add_column(
            "investigations",
            sa.Column("suspicious_count", sa.Integer(), nullable=False, server_default="0"),
        )

    # Check if clean_count column exists
    result = connection.execute(
        sa.text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='investigations' AND column_name='clean_count'
        """)
    )
    if not result.fetchone():
        op.add_column(
            "investigations",
            sa.Column("clean_count", sa.Integer(), nullable=False, server_default="0"),
        )

    # Check if verdict_reasoning column exists
    result = connection.execute(
        sa.text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='investigations' AND column_name='verdict_reasoning'
        """)
    )
    if not result.fetchone():
        op.add_column(
            "investigations",
            sa.Column("verdict_reasoning", sa.Text(), nullable=True),
        )

    # Check if pending_reviews table exists before creating
    result = connection.execute(
        sa.text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name='pending_reviews'
        """)
    )
    if not result.fetchone():
        # Create pending_reviews table
        op.create_table(
            "pending_reviews",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("max_severity", sa.String(length=20), nullable=False),
            sa.Column("alert_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("malicious_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("suspicious_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("clean_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("findings", postgresql.ARRAY(sa.Text()), nullable=False),
            sa.Column("enrichments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("misp_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("ai_decision", sa.String(length=50), nullable=True),
            sa.Column("ai_confidence", sa.Float(), nullable=True),
            sa.Column("ai_assessment", sa.Text(), nullable=True),
            sa.Column("ai_recommendation", sa.Text(), nullable=True),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("responded_at", sa.DateTime(), nullable=True),
            sa.Column("reviewer", sa.String(length=255), nullable=True),
            sa.Column("feedback", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_pending_reviews_status", "pending_reviews", ["status"], unique=False)
        op.create_index("ix_pending_reviews_created_at", "pending_reviews", ["created_at"], unique=False)
        op.create_index("ix_pending_reviews_investigation_id", "pending_reviews", ["investigation_id"], unique=False)


def downgrade() -> None:
    # Drop pending_reviews table and indexes
    op.drop_index("ix_pending_reviews_investigation_id", table_name="pending_reviews")
    op.drop_index("ix_pending_reviews_created_at", table_name="pending_reviews")
    op.drop_index("ix_pending_reviews_status", table_name="pending_reviews")
    op.drop_table("pending_reviews")

    # Remove added columns from investigations
    op.drop_column("investigations", "verdict_reasoning")
    op.drop_column("investigations", "clean_count")
    op.drop_column("investigations", "suspicious_count")
