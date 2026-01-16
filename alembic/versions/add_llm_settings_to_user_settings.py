"""Add LLM preference fields to user_settings.

Revision ID: add_llm_settings_to_user_settings
Revises: add_workflow_resumed_at
Create Date: 2026-01-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_llm_settings_to_user_settings"
down_revision: str | None = "add_workflow_resumed_at"
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
    columns: dict[str, sa.Column] = {
        "llm_provider": sa.Column(
            "llm_provider",
            sa.String(length=20),
            nullable=False,
            server_default="anthropic",
        ),
        "llm_fast_model": sa.Column(
            "llm_fast_model",
            sa.String(length=255),
            nullable=False,
            server_default="claude-sonnet-4-20250514",
        ),
        "llm_reasoning_model": sa.Column(
            "llm_reasoning_model",
            sa.String(length=255),
            nullable=False,
            server_default="claude-sonnet-4-20250514",
        ),
        "llm_temperature": sa.Column(
            "llm_temperature",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        "llm_max_tokens": sa.Column(
            "llm_max_tokens",
            sa.Integer(),
            nullable=False,
            server_default="4096",
        ),
        "llm_anthropic_base_url": sa.Column("llm_anthropic_base_url", sa.String(length=500), nullable=True),
        "llm_openai_base_url": sa.Column("llm_openai_base_url", sa.String(length=500), nullable=True),
        "llm_openai_organization": sa.Column("llm_openai_organization", sa.String(length=255), nullable=True),
    }

    for name, column in columns.items():
        if not _column_exists(connection, "user_settings", name):
            op.add_column("user_settings", column)


def downgrade() -> None:
    connection = op.get_bind()
    for column in (
        "llm_openai_organization",
        "llm_openai_base_url",
        "llm_anthropic_base_url",
        "llm_max_tokens",
        "llm_temperature",
        "llm_reasoning_model",
        "llm_fast_model",
        "llm_provider",
    ):
        if _column_exists(connection, "user_settings", column):
            op.drop_column("user_settings", column)

