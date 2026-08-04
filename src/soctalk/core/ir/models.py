"""SQLModel definitions for the native IR subsystem.

Paired with alembic ``v1_0003_ir_core``. Keep fields minimal; DB-side
defaults in the migration carry the boilerplate.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# Enums (app-side; DB stores as text with CHECK)
# ---------------------------------------------------------------------------


class Visibility(str, Enum):
    MSSP_ONLY = "mssp_only"
    CUSTOMER_SAFE = "customer_safe"
    SYSTEM = "system"
    TOOL_OUTPUT = "tool_output"


class AlertStatus(str, Enum):
    NEW = "new"
    ACKED = "acked"
    PROMOTED = "promoted"
    IGNORED = "ignored"
    AUTO_CLOSED = "auto_closed"
    # Matched a declared engagement (pentest/red-team) window in-scope: handled
    # by deconfliction, out of the open queue, but NEVER closed/FP.
    DECONFLICTED = "deconflicted"


class AIAssessment(str, Enum):
    REAL = "real"
    UNCLEAR = "unclear"
    LIKELY_FP = "likely_fp"
    HIGH_CONF_FP = "high_conf_fp"


class CaseStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"
    AUTO_CLOSED_FP = "auto_closed_fp"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    WAITING_ON_GATE = "waiting_on_gate"
    HALTED_BUDGET = "halted_budget"
    COMPLETED = "completed"
    FAILED = "failed"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    EXECUTED = "executed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class CapabilityClass(str, Enum):
    READ_LOCAL = "read_local"
    READ_EXTERNAL_SILENT = "read_external_silent"
    READ_EXTERNAL_ATTRIBUTED = "read_external_attributed"
    WRITE_SANDBOX = "write_sandbox"
    WRITE_EXTERNAL = "write_external"


class IOCType(str, Enum):
    IP = "ip"
    IPV6 = "ipv6"
    DOMAIN = "domain"
    URL = "url"
    HASH_MD5 = "hash_md5"
    HASH_SHA1 = "hash_sha1"
    HASH_SHA256 = "hash_sha256"
    FILENAME = "filename"
    EMAIL = "email"
    USER_AGENT = "user_agent"
    REGISTRY = "registry"
    MUTEX = "mutex"
    HOSTNAME = "hostname"
    PROCESS = "process"


class TLP(str, Enum):
    CLEAR = "clear"
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Alert(SQLModel, table=True):
    __tablename__ = "alerts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    source: str
    rule_id: str | None = None
    severity: int
    signature: str
    first_event_at: datetime
    last_event_at: datetime
    event_count: int = 1
    source_event_ids: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    asset_ids: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    initial_iocs: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    ai_assessment: str | None = None
    ai_confidence: float | None = None
    status: str = AlertStatus.NEW.value
    investigation_id: UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
    )
    visibility: str = Visibility.MSSP_ONLY.value
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Investigation(SQLModel, table=True):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "short_id", name="uq_cases_tenant_short_id"),
        Index("ix_cases_tenant_status", "tenant_id", "status"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    short_id: str
    title: str
    status: str = CaseStatus.ACTIVE.value
    severity: int
    assignee_user_id: UUID | None = None
    summary: str | None = None
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    close_reason: str | None = None
    reopen_window_until: datetime | None = None
    reopen_signature: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    reopen_count: int = 0
    visibility: str = Visibility.MSSP_ONLY.value
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class InvestigationRun(SQLModel, table=True):
    __tablename__ = "investigation_runs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    status: str = RunStatus.ACTIVE.value
    tokens_used: int = 0
    tokens_budget: int = 200_000
    dollars_used: float = 0.0
    dollars_budget: float = 5.0
    tool_calls_used: int = 0
    tool_calls_budget: int = 200
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    last_error: str | None = None
    # Transient-retry bookkeeping (issue #77, migration v1_0039). The worker
    # path reads and writes these through raw SQL, which is why the drift
    # between this model and the schema went unnoticed: nothing broke until
    # somebody read retry state through the ORM. Defaults mirror the
    # migration's server defaults.
    attempts: int = 0
    max_attempts: int = 4
    last_error_category: str | None = None


class InvestigationEvent(SQLModel, table=True):
    __tablename__ = "investigation_events"
    __table_args__ = (
        UniqueConstraint("investigation_id", "idempotency_key", name="uq_case_events_idempotency"),
        Index("ix_case_events_case_seq", "investigation_id", "seq"),
    )

    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    run_id: UUID | None = None
    seq: int | None = None  # Filled by DB default (nextval)
    kind: str
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    causation_event_id: UUID | None = None
    correlation_id: UUID | None = None
    idempotency_key: str
    visibility: str = Visibility.MSSP_ONLY.value
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CaseFacts(SQLModel, table=True):
    """Reducer-owned projection of the investigation's structured state."""

    __tablename__ = "investigation_facts"

    investigation_id: UUID = Field(
        sa_column=Column(
            ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True
        )
    )
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    hypotheses: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    active_directives: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    active_policies: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    timeline_summary: list[Any] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    applied_seq: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IOC(SQLModel, table=True):
    __tablename__ = "iocs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_iocs_fingerprint"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    type: str
    value: str
    fingerprint: str
    tlp: str = TLP.AMBER.value
    pap: str = TLP.AMBER.value
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    external_context: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    visibility: str = Visibility.MSSP_ONLY.value


class CaseIOC(SQLModel, table=True):
    __tablename__ = "investigation_iocs"

    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(
            ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True
        )
    )
    ioc_id: UUID = Field(
        sa_column=Column(
            ForeignKey("iocs.id", ondelete="CASCADE"), primary_key=True
        )
    )
    added_at: datetime = Field(default_factory=datetime.utcnow)
    added_by: str


class InvestigationAsset(SQLModel, table=True):
    __tablename__ = "investigation_assets"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    kind: str
    identifier: str
    details: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    added_at: datetime = Field(default_factory=datetime.utcnow)


class InvestigationLink(SQLModel, table=True):
    __tablename__ = "investigation_links"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    from_investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    to_investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    link_kind: str
    signature: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    confidence: float = 0.5
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    visibility: str = Visibility.MSSP_ONLY.value


class Note(SQLModel, table=True):
    __tablename__ = "notes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    author_kind: str  # 'ai' | 'human' | 'system'
    author_id: str | None = None
    body: str
    visibility: str = Visibility.MSSP_ONLY.value
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Proposal(SQLModel, table=True):
    __tablename__ = "proposals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID = Field(
        sa_column=Column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    )
    run_id: UUID | None = None
    action_type: str
    params: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    rationale: str = ""
    blast_radius: str | None = None
    capability_class: str
    status: str = ProposalStatus.DRAFT.value
    idempotency_key: str
    approver_user_id: UUID | None = None
    approval_reason: str | None = None
    rejected_reason: str | None = None
    visibility: str = Visibility.MSSP_ONLY.value
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class InvestigationOutbox(SQLModel, table=True):
    __tablename__ = "investigation_outbox"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID | None = None
    kind: str
    idempotency_key: str
    payload: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    external_system: str | None = None
    external_ref: str | None = None
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 5
    last_error: str | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None
    next_attempt_at: datetime = Field(default_factory=datetime.utcnow)
    succeeded_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExecutionLog(SQLModel, table=True):
    __tablename__ = "execution_log"

    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    investigation_id: UUID | None = None
    run_id: UUID | None = None
    actor_kind: str
    actor_id: str
    kind: str
    subject_type: str | None = None
    subject_id: str | None = None
    before: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    after: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    versions: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    ts: datetime = Field(default_factory=datetime.utcnow)


class TenantPolicy(SQLModel, table=True):
    __tablename__ = "tenant_policies"

    tenant_id: UUID = Field(
        sa_column=Column(
            ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
        )
    )
    key: str = Field(primary_key=True)
    value: Any = Field(default=None, sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AuthorizationFactRow(SQLModel, table=True):
    """Durable, tenant-scoped store for a typed AuthorizationFact (models.authorization).

    The full validated fact rides in ``body`` (JSONB); the envelope fields are lifted into
    columns for lookup. Revocation is soft (``revoked_at`` set, never a hard delete) so the
    audit trail survives, and a superseding fact points back via ``superseded_by``. The run
    loader consumes this store-primary (paired with alembic ``v1_0034_authorization_facts``).
    """

    __tablename__ = "authorization_facts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fact_id", name="uq_authz_facts_tenant_factid"),
        Index("ix_authz_facts_tenant_current", "tenant_id", "revoked_at"),
        Index("ix_authz_facts_tenant_scope", "tenant_id", "subject", "target", "action"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    fact_id: str  # the fact's own envelope id; unique per tenant
    kind: str  # grant | prohibition | change_freeze | entity_context
    track: str  # account | fim
    source_type: str  # telemetry_routine | analyst_asserted | system_asserted | connector_verified
    trust: int
    # queryable scope, lifted from the envelope (kind-dependent; nullable)
    subject: str | None = None
    target: str | None = None
    action: str | None = None
    entity_name: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    superseded_by: str | None = None
    # lifecycle / revocation (soft-delete)
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None
    revoke_reason: str | None = None
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # the full validated fact, AUTHORIZATION_FACT_ADAPTER.dump_python(mode="json")
    body: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
