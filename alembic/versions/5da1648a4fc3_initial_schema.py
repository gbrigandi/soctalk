"""initial schema

Revision ID: 5da1648a4fc3
Revises:
Create Date: 2025-12-09 15:13:06.138419

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5da1648a4fc3"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Events table (append-only event store)
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_aggregate_id", "events", ["aggregate_id"], unique=False)
    op.create_index("ix_events_event_type", "events", ["event_type"], unique=False)
    op.create_index("ix_events_timestamp", "events", ["timestamp"], unique=False)
    op.create_index("ix_events_idempotency_key", "events", ["idempotency_key"], unique=True)
    op.create_unique_constraint("uq_aggregate_version", "events", ["aggregate_id", "version"])

    # Investigations read model
    op.create_table(
        "investigations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("phase", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("time_to_triage_seconds", sa.Integer(), nullable=True),
        sa.Column("time_to_verdict_seconds", sa.Integer(), nullable=True),
        sa.Column("alert_count", sa.Integer(), nullable=False),
        sa.Column("observable_count", sa.Integer(), nullable=False),
        sa.Column("malicious_count", sa.Integer(), nullable=False),
        sa.Column("max_severity", sa.String(length=20), nullable=True),
        sa.Column("verdict_decision", sa.String(length=50), nullable=True),
        sa.Column("verdict_confidence", sa.Float(), nullable=True),
        sa.Column("thehive_case_id", sa.String(length=100), nullable=True),
        sa.Column("threat_actor", sa.String(length=255), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Hourly metrics
    op.create_table(
        "metrics_hourly",
        sa.Column("hour", sa.DateTime(), nullable=False),
        sa.Column("investigations_created", sa.Integer(), nullable=False),
        sa.Column("investigations_closed", sa.Integer(), nullable=False),
        sa.Column("escalations", sa.Integer(), nullable=False),
        sa.Column("auto_closed", sa.Integer(), nullable=False),
        sa.Column("avg_time_to_verdict_seconds", sa.Integer(), nullable=True),
        sa.Column("total_alerts", sa.Integer(), nullable=False),
        sa.Column("total_observables", sa.Integer(), nullable=False),
        sa.Column("malicious_observables", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("hour"),
    )

    # IOC statistics
    op.create_table(
        "ioc_stats",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("value", sa.String(length=1000), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("malicious_count", sa.Integer(), nullable=False),
        sa.Column("benign_count", sa.Integer(), nullable=False),
        sa.Column("threat_actors", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ioc_stats_value_type", "ioc_stats", ["value", "type"], unique=False)

    # Rule statistics
    op.create_table(
        "rule_stats",
        sa.Column("rule_id", sa.String(length=50), nullable=False),
        sa.Column("times_triggered", sa.Integer(), nullable=False),
        sa.Column("escalation_count", sa.Integer(), nullable=False),
        sa.Column("auto_close_count", sa.Integer(), nullable=False),
        sa.Column("precision_rate", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("rule_id"),
    )

    # Analyzer statistics
    op.create_table(
        "analyzer_stats",
        sa.Column("analyzer", sa.String(length=100), nullable=False),
        sa.Column("invocations", sa.Integer(), nullable=False),
        sa.Column("successes", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("avg_response_time_ms", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("analyzer"),
    )


def downgrade() -> None:
    op.drop_table("analyzer_stats")
    op.drop_table("rule_stats")
    op.drop_index("ix_ioc_stats_value_type", table_name="ioc_stats")
    op.drop_table("ioc_stats")
    op.drop_table("metrics_hourly")
    op.drop_table("investigations")
    op.drop_constraint("uq_aggregate_version", "events", type_="unique")
    op.drop_index("ix_events_idempotency_key", table_name="events")
    op.drop_index("ix_events_timestamp", table_name="events")
    op.drop_index("ix_events_event_type", table_name="events")
    op.drop_index("ix_events_aggregate_id", table_name="events")
    op.drop_table("events")
