"""Event types for the event store."""

from enum import Enum


class EventType(str, Enum):
    """Types of events that can occur in the system."""

    # Investigation lifecycle
    INVESTIGATION_CREATED = "investigation.created"
    INVESTIGATION_STARTED = "investigation.started"
    INVESTIGATION_CLOSED = "investigation.closed"
    INVESTIGATION_PAUSED = "investigation.paused"
    INVESTIGATION_RESUMED = "investigation.resumed"
    INVESTIGATION_CANCELLED = "investigation.cancelled"
    INVESTIGATION_ESCALATED = "investigation.escalated"
    INVESTIGATION_AUTO_CLOSED = "investigation.auto_closed"

    # Alert management
    ALERT_ADDED = "alert.added"
    ALERT_CORRELATED = "alert.correlated"

    # Observable extraction and enrichment
    OBSERVABLE_EXTRACTED = "observable.extracted"
    ENRICHMENT_REQUESTED = "enrichment.requested"
    ENRICHMENT_COMPLETED = "enrichment.completed"
    ENRICHMENT_FAILED = "enrichment.failed"

    # Supervisor decisions
    SUPERVISOR_DECISION = "supervisor.decision"
    PHASE_CHANGED = "phase.changed"

    # Verdict
    VERDICT_RENDERED = "verdict.rendered"

    # Human-in-the-loop
    HUMAN_REVIEW_REQUESTED = "human.review_requested"
    HUMAN_DECISION_RECEIVED = "human.decision_received"

    # TheHive integration
    THEHIVE_CASE_CREATED = "thehive.case_created"
    THEHIVE_ALERT_PROMOTED = "thehive.alert_promoted"

    # MISP integration
    MISP_IOC_MATCHED = "misp.ioc_matched"
    MISP_CONTEXT_RETRIEVED = "misp.context_retrieved"

    # Analyzers
    ANALYZER_INVOKED = "analyzer.invoked"
    ANALYZER_COMPLETED = "analyzer.completed"

    # Errors
    ERROR_OCCURRED = "error.occurred"
