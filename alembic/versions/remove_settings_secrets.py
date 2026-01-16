"""Remove secret fields from user_settings.

Revision ID: remove_settings_secrets
Revises: add_missing_columns
Create Date: 2026-01-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "remove_settings_secrets"
down_revision: str | None = "add_missing_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SECRET_COLUMNS: list[str] = [
    "wazuh_username",
    "wazuh_password",
    "cortex_api_key",
    "thehive_api_key",
    "misp_api_key",
    "slack_webhook_url",
]


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
    for column in _SECRET_COLUMNS:
        if _column_exists(connection, "user_settings", column):
            op.drop_column("user_settings", column)


def downgrade() -> None:
    connection = op.get_bind()

    columns: dict[str, sa.Column] = {
        "wazuh_username": sa.Column("wazuh_username", sa.String(length=255), nullable=True),
        "wazuh_password": sa.Column("wazuh_password", sa.String(length=255), nullable=True),
        "cortex_api_key": sa.Column("cortex_api_key", sa.String(length=255), nullable=True),
        "thehive_api_key": sa.Column("thehive_api_key", sa.String(length=255), nullable=True),
        "misp_api_key": sa.Column("misp_api_key", sa.String(length=255), nullable=True),
        "slack_webhook_url": sa.Column("slack_webhook_url", sa.String(length=500), nullable=True),
    }

    for name, column in columns.items():
        if not _column_exists(connection, "user_settings", name):
            op.add_column("user_settings", column)
