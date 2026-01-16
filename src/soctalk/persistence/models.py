"""SQLModel table definitions for persistence."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel, Text


class Event(SQLModel, table=True):
    """Append-only event store table."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("aggregate_id", "version", name="uq_aggregate_version"),
        Index("ix_events_aggregate_id", "aggregate_id"),
        Index("ix_events_event_type", "event_type"),
        Index("ix_events_timestamp", "timestamp"),
        Index("ix_events_idempotency_key", "idempotency_key", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    aggregate_id: UUID = Field()  # Index defined in __table_args__
    aggregate_type: str = Field(default="Investigation", max_length=100)
    event_type: str = Field(max_length=100)
    version: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    event_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    idempotency_key: str | None = Field(default=None, max_length=255)


class InvestigationReadModel(SQLModel, table=True):
    """Read model for investigation state (projection)."""

    __tablename__ = "investigations"

    id: UUID = Field(primary_key=True)
    title: str | None = Field(default=None, max_length=500)
    status: str = Field(default="pending", max_length=50)
    phase: str = Field(default="triage", max_length=50)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: datetime | None = Field(default=None)
    time_to_triage_seconds: int | None = Field(default=None)
    time_to_verdict_seconds: int | None = Field(default=None)
    alert_count: int = Field(default=0)
    observable_count: int = Field(default=0)
    malicious_count: int = Field(default=0)
    suspicious_count: int = Field(default=0)
    clean_count: int = Field(default=0)
    max_severity: str | None = Field(default=None, max_length=20)
    verdict_decision: str | None = Field(default=None, max_length=50)
    verdict_confidence: float | None = Field(default=None)
    verdict_reasoning: str | None = Field(default=None, sa_column=Column(Text))
    thehive_case_id: str | None = Field(default=None, max_length=100)
    threat_actor: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(Text)))


class MetricsHourly(SQLModel, table=True):
    """Hourly aggregated metrics."""

    __tablename__ = "metrics_hourly"

    hour: datetime = Field(primary_key=True)
    investigations_created: int = Field(default=0)
    investigations_closed: int = Field(default=0)
    escalations: int = Field(default=0)
    auto_closed: int = Field(default=0)
    avg_time_to_verdict_seconds: int | None = Field(default=None)
    total_alerts: int = Field(default=0)
    total_observables: int = Field(default=0)
    malicious_observables: int = Field(default=0)


class IOCStats(SQLModel, table=True):
    """IOC statistics."""

    __tablename__ = "ioc_stats"
    __table_args__ = (
        Index("ix_ioc_stats_value_type", "value", "type"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    value: str = Field(max_length=1000)
    type: str = Field(max_length=50)
    times_seen: int = Field(default=1)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    malicious_count: int = Field(default=0)
    benign_count: int = Field(default=0)
    threat_actors: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(Text)))


class RuleStats(SQLModel, table=True):
    """Wazuh rule statistics."""

    __tablename__ = "rule_stats"

    rule_id: str = Field(primary_key=True, max_length=50)
    times_triggered: int = Field(default=0)
    escalation_count: int = Field(default=0)
    auto_close_count: int = Field(default=0)
    precision_rate: float | None = Field(default=None)


class AnalyzerStats(SQLModel, table=True):
    """Cortex analyzer statistics."""

    __tablename__ = "analyzer_stats"

    analyzer: str = Field(primary_key=True, max_length=100)
    invocations: int = Field(default=0)
    successes: int = Field(default=0)
    failures: int = Field(default=0)
    avg_response_time_ms: float | None = Field(default=None)


class UserSettings(SQLModel, table=True):
    """User preferences and settings."""

    __tablename__ = "user_settings"

    id: str = Field(default="default", primary_key=True, max_length=100)

    # Integration settings - Wazuh SIEM
    wazuh_enabled: bool = Field(default=False)
    wazuh_url: str | None = Field(default=None, max_length=500)
    wazuh_verify_ssl: bool = Field(default=True)

    # Integration settings - Cortex (analysis/enrichment)
    cortex_enabled: bool = Field(default=False)
    cortex_url: str | None = Field(default=None, max_length=500)
    cortex_verify_ssl: bool = Field(default=True)

    # Integration settings - TheHive (incident response)
    thehive_enabled: bool = Field(default=False)
    thehive_url: str | None = Field(default=None, max_length=500)
    thehive_organisation: str | None = Field(default=None, max_length=255)
    thehive_verify_ssl: bool = Field(default=True)

    # Integration settings - MISP (threat intelligence)
    misp_enabled: bool = Field(default=False)
    misp_url: str | None = Field(default=None, max_length=500)
    misp_verify_ssl: bool = Field(default=True)

    # Integration settings - Slack (notifications)
    slack_enabled: bool = Field(default=False)
    slack_channel: str | None = Field(default=None, max_length=100)
    slack_notify_on_escalation: bool = Field(default=True)
    slack_notify_on_verdict: bool = Field(default=True)

    # LLM settings (non-secret; secrets are env-only)
    llm_provider: str = Field(default="anthropic", max_length=20)
    llm_fast_model: str = Field(default="claude-sonnet-4-20250514", max_length=255)
    llm_reasoning_model: str = Field(default="claude-sonnet-4-20250514", max_length=255)
    llm_temperature: float = Field(default=0.0)
    llm_max_tokens: int = Field(default=4096)
    llm_anthropic_base_url: str | None = Field(default=None, max_length=500)
    llm_openai_base_url: str | None = Field(default=None, max_length=500)
    llm_openai_organization: str | None = Field(default=None, max_length=255)

    # Timestamps
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PendingReview(SQLModel, table=True):
    """Pending human-in-the-loop review requests."""

    __tablename__ = "pending_reviews"
    __table_args__ = (
        Index("ix_pending_reviews_status", "status"),
        Index("ix_pending_reviews_created_at", "created_at"),
        Index("ix_pending_reviews_investigation_id", "investigation_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    investigation_id: UUID = Field()  # Index defined in __table_args__
    # pending, approved, rejected, info_requested, expired
    status: str = Field(default="pending", max_length=50)
    title: str = Field(max_length=500)
    description: str = Field(sa_column=Column(Text))
    max_severity: str = Field(max_length=20)
    alert_count: int = Field(default=0)
    malicious_count: int = Field(default=0)
    suspicious_count: int = Field(default=0)
    clean_count: int = Field(default=0)
    findings: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(Text)))
    enrichments: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    misp_context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    ai_decision: str | None = Field(default=None, max_length=50)
    ai_confidence: float | None = Field(default=None)
    ai_assessment: str | None = Field(default=None, sa_column=Column(Text))
    ai_recommendation: str | None = Field(default=None, sa_column=Column(Text))
    timeout_seconds: int = Field(default=300)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = Field(default=None)
    responded_at: datetime | None = Field(default=None)
    reviewer: str | None = Field(default=None, max_length=255)
    feedback: str | None = Field(default=None, sa_column=Column(Text))
    workflow_resumed_at: datetime | None = Field(default=None)
