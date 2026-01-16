"""Data models for Human-in-the-Loop interactions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from soctalk.models.enums import HumanDecision, Severity, VerdictDecision, ImpactLevel, Urgency


class EnrichmentSummary(BaseModel):
    """Summary of an enrichment result for display in HIL."""

    observable_value: str
    observable_type: str
    analyzer: str
    verdict: str
    confidence: float
    details: dict[str, Any] = Field(default_factory=dict)


class MISPContextSummary(BaseModel):
    """Summary of MISP threat intelligence context for display in HIL."""

    iocs_checked: int = 0
    iocs_matched: int = 0
    threat_actors: list[str] = Field(default_factory=list)
    campaigns: list[str] = Field(default_factory=list)
    warninglist_hits: int = 0
    matched_events: list[str] = Field(default_factory=list)  # Event IDs


class HILRequest(BaseModel):
    """Request for human review of an investigation."""

    investigation_id: str
    title: str
    description: str
    max_severity: Severity
    alert_count: int
    created_at: datetime

    # Threat intelligence summary
    malicious_count: int = 0
    suspicious_count: int = 0
    clean_count: int = 0

    # Key findings
    findings: list[str] = Field(default_factory=list)

    # Enrichment results from Cortex
    enrichments: list[EnrichmentSummary] = Field(default_factory=list)

    # MISP threat intelligence context
    misp_context: Optional[MISPContextSummary] = None

    # AI verdict (if available)
    ai_decision: Optional[VerdictDecision] = None
    ai_confidence: Optional[float] = None
    ai_impact: Optional[ImpactLevel] = None
    ai_urgency: Optional[Urgency] = None
    ai_assessment: Optional[str] = None
    ai_recommendation: Optional[str] = None
    ai_evidence: list[str] = Field(default_factory=list)

    # Request metadata
    timeout_seconds: int = 300  # 5 minutes default
    channel: Optional[str] = None  # Target channel/conversation


class HILResponse(BaseModel):
    """Response from human review."""

    investigation_id: str
    decision: HumanDecision
    reviewer: str
    feedback: Optional[str] = None
    responded_at: datetime = Field(default_factory=datetime.now)

    # Metadata
    response_time_seconds: Optional[float] = None
    backend: str = "unknown"  # Which backend was used (slack, discord, cli)
