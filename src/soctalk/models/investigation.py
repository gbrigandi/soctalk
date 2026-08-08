"""InvestigationRunState and finding models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from soctalk.models.enums import Severity, InvestigationStatus
from soctalk.models.alerts import Alert
from soctalk.models.authorization import AuthorizationContext
from soctalk.models.observables import Observable, EnrichmentResult


class Finding(BaseModel):
    """A finding or conclusion from investigation analysis."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    description: str = Field(..., description="Description of the finding")
    severity: Severity = Field(..., description="Severity of the finding")
    evidence: list[str] = Field(default_factory=list, description="Evidence supporting the finding")
    recommendations: list[str] = Field(
        default_factory=list, description="Recommended actions"
    )
    mitre_tactics: list[str] = Field(
        default_factory=list, description="Related MITRE ATT&CK tactics"
    )
    mitre_techniques: list[str] = Field(
        default_factory=list, description="Related MITRE ATT&CK techniques"
    )
    created_at: datetime = Field(default_factory=datetime.now)

    def to_summary(self) -> str:
        """Generate a human-readable summary of the finding.

        Returns:
            Summary string.
        """
        severity_emoji = {
            Severity.LOW: "🟢",
            Severity.MEDIUM: "🟡",
            Severity.HIGH: "🟠",
            Severity.CRITICAL: "🔴",
        }
        emoji = severity_emoji.get(self.severity, "⚪")

        lines = [f"{emoji} [{self.severity.value.upper()}] {self.description}"]

        if self.evidence:
            lines.append("   Evidence:")
            for e in self.evidence[:3]:
                lines.append(f"   - {e}")

        if self.recommendations:
            lines.append("   Recommendations:")
            for r in self.recommendations[:3]:
                lines.append(f"   - {r}")

        return "\n".join(lines)


class InvestigationRunState(BaseModel):
    """In-memory state of one AI investigation run.

    This is the shape passed through the LangGraph state machine during
    a single run — alerts being correlated, observables being enriched,
    findings accumulating. It is NOT the persistent InvestigationRunState entity
    (that lives as a row in the ``investigations`` table, modeled by
    ``soctalk.core.ir.models.InvestigationRunState``). The two were conflated
    pre-v1; the rename disambiguates them.

    Renaming the V0 ``InvestigationRunState`` class to ``InvestigationRunState``
    freed the ``InvestigationRunState`` name for the persistent record. This
    class continues to drive the legacy LangGraph runs-worker; over
    time, the worker should consume the persistent InvestigationRunState +
    case_run rows directly and this transient state becomes derived,
    not authoritative.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = Field(default="Untitled InvestigationRunState", description="InvestigationRunState title")
    description: Optional[str] = Field(default=None, description="InvestigationRunState description")
    alerts: list[Alert] = Field(default_factory=list, description="Correlated alerts")
    observables: list[Observable] = Field(
        default_factory=list, description="All observables from alerts"
    )
    enrichments: list[EnrichmentResult] = Field(
        default_factory=list, description="Enrichment results"
    )
    findings: list[Finding] = Field(default_factory=list, description="InvestigationRunState findings")
    status: InvestigationStatus = Field(
        default=InvestigationStatus.PENDING, description="Current status"
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    closed_at: Optional[datetime] = Field(None, description="When investigation was closed")
    closure_reason: Optional[str] = Field(None, description="Reason for closure")
    thehive_case_id: Optional[str] = Field(None, description="TheHive case ID if escalated")
    misp_context: Optional[dict[str, Any]] = Field(
        default=None, description="MISP threat intelligence context"
    )
    authorization_context: Optional[AuthorizationContext] = Field(
        default=None,
        description="Typed authorization/expectedness facts around the alerted activity",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    @property
    def max_severity(self) -> Severity:
        """Get the maximum severity from all alerts.

        Returns:
            Maximum severity level.
        """
        if not self.alerts:
            return Severity.LOW

        severity_order = {
            Severity.LOW: 0,
            Severity.MEDIUM: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }

        max_sev = max(self.alerts, key=lambda a: severity_order.get(a.severity, 0))
        return max_sev.severity

    @property
    def pending_observables(self) -> list[Observable]:
        """Get observables that haven't been enriched yet.

        Returns:
            List of unenriched observables.
        """
        enriched_values = {e.observable.value for e in self.enrichments}
        return [o for o in self.observables if o.value not in enriched_values]

    @property
    def enriched_observables(self) -> list[Observable]:
        """Get observables that have been enriched.

        Returns:
            List of enriched observables.
        """
        enriched_values = {e.observable.value for e in self.enrichments}
        return [o for o in self.observables if o.value in enriched_values]

    @property
    def malicious_indicators(self) -> list[EnrichmentResult]:
        """Get enrichments with malicious verdict.

        Returns:
            List of malicious enrichment results.
        """
        return [e for e in self.enrichments if e.is_malicious]

    @property
    def suspicious_indicators(self) -> list[EnrichmentResult]:
        """Get enrichments with suspicious or malicious verdict.

        Returns:
            List of suspicious enrichment results.
        """
        return [e for e in self.enrichments if e.is_suspicious]

    def generate_title(self) -> str:
        """Generate a descriptive title based on alerts.

        Returns:
            Generated title.
        """
        if not self.alerts:
            return "Empty InvestigationRunState"

        # Find the most descriptive alert (skip generic ones)
        generic_descriptions = {
            "no description available",
            "no description",
            "",
        }

        best_description = None
        for alert in self.alerts:
            desc = alert.rule_description.strip()
            if desc.lower() not in generic_descriptions:
                best_description = desc
                break

        # Fall back to first alert if all are generic
        if not best_description:
            best_description = self.alerts[0].rule_description or "Security Alert"

        base = best_description[:50]

        if len(self.alerts) > 1:
            return f"{base} (+{len(self.alerts) - 1} related alerts)"

        return base

    def to_thehive_case_data(self) -> dict[str, Any]:
        """Generate data for creating a TheHive case.

        Returns:
            Dictionary suitable for create_thehive_case tool.
        """
        # Build description
        description_parts = [
            "## InvestigationRunState Summary",
            "",
            f"**InvestigationRunState ID:** {self.id}",
            f"**Created:** {self.created_at.isoformat()}",
            "",
            "## Alerts",
            "",
        ]

        for alert in self.alerts:
            description_parts.append(f"- **{alert.rule_description}**")
            description_parts.append(f"  - Alert ID: {alert.id}")
            description_parts.append(f"  - Severity: {alert.severity.value}")
            description_parts.append(f"  - Agent: {alert.source.agent_name}")
            description_parts.append("")

        if self.findings:
            description_parts.append("## Findings")
            description_parts.append("")
            for finding in self.findings:
                description_parts.append(f"### {finding.description}")
                description_parts.append(f"Severity: {finding.severity.value}")
                if finding.evidence:
                    description_parts.append("Evidence:")
                    for e in finding.evidence:
                        description_parts.append(f"- {e}")
                description_parts.append("")

        if self.enrichments:
            description_parts.append("## Threat Intelligence")
            description_parts.append("")
            for e in self.enrichments:
                description_parts.append(
                    f"- **{e.observable.value}** ({e.observable.type.value}): "
                    f"{e.verdict.value} via {e.analyzer}"
                )
            description_parts.append("")

        if self.misp_context:
            description_parts.append("## MISP Context")
            description_parts.append("")

            matches = self.misp_context.get("matches", [])
            threat_actors = self.misp_context.get("threat_actors", [])
            campaigns = self.misp_context.get("campaigns", [])
            warninglist_hits = self.misp_context.get("warninglist_hits", [])
            checked_iocs = self.misp_context.get("checked_iocs", [])
            status = self.misp_context.get("status")

            # An unreachable lookup must never render as "0 matches" (#122).
            # That reads as evidence the indicator is unknown to threat intel,
            # and the supervisor cited exactly that phrasing to close a
            # brute-force-then-success as a false positive while MISP was down.
            # Absence of evidence and evidence of absence get different words.
            if status in ("unavailable", "degraded"):
                reason = self.misp_context.get("unavailable_reason") or ""
                failed = self.misp_context.get("failed_checks") or []
                if status == "unavailable":
                    description_parts.append(
                        "**THREAT INTELLIGENCE UNAVAILABLE — no lookup was performed.**"
                    )
                    description_parts.append(
                        "Treat this as MISSING evidence, not as an absence of matches. "
                        "Do not conclude an indicator is unknown to threat intelligence."
                    )
                else:
                    description_parts.append(
                        f"**THREAT INTELLIGENCE PARTIALLY UNAVAILABLE — "
                        f"{len(failed)} of {len(failed) + len(checked_iocs)} lookups failed.**"
                    )
                    description_parts.append(
                        "Matches below cover only the indicators that were reachable."
                    )
                if reason:
                    description_parts.append(f"Reason: {reason}")
                description_parts.append("")
            elif status == "not_configured":
                description_parts.append(
                    "No MISP server is configured for this tenant, so threat "
                    "intelligence was not part of this investigation."
                )
                description_parts.append("")
            else:
                description_parts.append(f"**IOCs checked:** {len(checked_iocs)}")
                description_parts.append(f"**IOC matches:** {len(matches)}")
                description_parts.append("")

            if threat_actors:
                description_parts.append("### Threat Actors")
                for ta in threat_actors[:5]:
                    description_parts.append(f"- {ta}")
                description_parts.append("")

            if campaigns:
                description_parts.append("### Campaigns")
                for campaign in campaigns[:5]:
                    description_parts.append(f"- {campaign}")
                description_parts.append("")

            if matches:
                description_parts.append("### IOC Matches")
                for m in matches[:10]:
                    event_ids = ", ".join(m.get("event_ids", [])[:3])
                    to_ids = " (IDS)" if m.get("to_ids") else ""
                    description_parts.append(
                        f"- **{m.get('value', 'unknown')}** ({m.get('type', '')}){to_ids}"
                    )
                    if event_ids:
                        description_parts.append(f"  - Events: {event_ids}")
                description_parts.append("")

            if warninglist_hits:
                description_parts.append("### Warninglist Hits (Potential False Positives)")
                for hit in warninglist_hits[:5]:
                    wls = ", ".join(hit.get("warninglists", []))
                    description_parts.append(f"- {hit.get('value', 'unknown')}: {wls}")
                description_parts.append("")

        if self.authorization_context is not None:
            # Local import: keep the models package a leaf (authorization.render imports models).
            from soctalk.authorization.render import to_authorization_case_lines

            description_parts.extend(to_authorization_case_lines(self.authorization_context))
            description_parts.append("")

        # Map severity
        severity_map = {
            Severity.LOW: 1,
            Severity.MEDIUM: 2,
            Severity.HIGH: 3,
            Severity.CRITICAL: 4,
        }

        return {
            "title": self.title or self.generate_title(),
            "description": "\n".join(description_parts),
            "severity": severity_map.get(self.max_severity, 2),
            "tags": self._generate_tags(),
            "tlp": 2,  # Amber by default
            "pap": 2,
        }

    def _generate_tags(self) -> list[str]:
        """Generate tags for TheHive case.

        Returns:
            List of tags.
        """
        tags = ["soctalk", f"severity:{self.max_severity.value}"]

        # Add observable type tags
        obs_types = set(o.type.value for o in self.observables)
        for t in obs_types:
            tags.append(f"ioc:{t}")

        # Add verdict tags
        if self.malicious_indicators:
            tags.append("verdict:malicious")
        elif self.suspicious_indicators:
            tags.append("verdict:suspicious")

        # Add MISP-related tags
        if self.misp_context:
            if self.misp_context.get("matches"):
                tags.append("misp:ioc-match")
            threat_actors = self.misp_context.get("threat_actors", [])
            for ta in threat_actors[:3]:
                # Sanitize tag (no spaces, lowercase)
                tags.append(f"ta:{ta.lower().replace(' ', '-')[:30]}")
            campaigns = self.misp_context.get("campaigns", [])
            for campaign in campaigns[:3]:
                tags.append(f"campaign:{campaign.lower().replace(' ', '-')[:30]}")
            if self.misp_context.get("warninglist_hits"):
                tags.append("misp:warninglist")

        return tags
