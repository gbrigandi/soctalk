"""Verdict model for reasoning LLM decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from soctalk.models.enums import (
    VerdictDecision,
    EvidenceStrength,
    ImpactLevel,
    Urgency,
)


class Verdict(BaseModel):
    """Structured verdict from the reasoning LLM.

    This represents the final decision gate before human review,
    produced by an advanced reasoning model.
    """

    decision: VerdictDecision = Field(
        ..., description="The verdict decision: escalate, close, or needs_more_info"
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the decision (0-1)"
    )

    # Threat assessment
    threat_assessment: str = Field(
        ..., description="Overall assessment of the threat"
    )
    evidence_strength: EvidenceStrength = Field(
        ..., description="Strength of evidence supporting the assessment"
    )

    # Risk analysis
    potential_impact: ImpactLevel = Field(
        ..., description="Potential impact if this is a true positive"
    )
    urgency: Urgency = Field(
        ..., description="Urgency level for response"
    )

    # Reasoning chain
    key_evidence: list[str] = Field(
        default_factory=list, description="Key pieces of evidence supporting the verdict"
    )
    gaps_in_evidence: list[str] = Field(
        default_factory=list, description="Gaps or missing evidence"
    )
    assumptions_made: list[str] = Field(
        default_factory=list, description="Assumptions made in the analysis"
    )
    alternative_explanations: list[str] = Field(
        default_factory=list, description="Alternative (benign) explanations considered"
    )

    # Final recommendation
    recommendation: str = Field(
        ..., description="Final recommendation with reasoning"
    )

    # If needs more info
    additional_investigation_needed: Optional[list[str]] = Field(
        None, description="What additional investigation is needed (if decision is needs_more_info)"
    )

    # Metadata
    reasoning_model: str = Field(
        default="unknown", description="Model used for reasoning"
    )
    timestamp: datetime = Field(default_factory=datetime.now)

    def to_summary(self) -> str:
        """Generate a human-readable summary of the verdict.

        Returns:
            Summary string.
        """
        decision_emoji = {
            VerdictDecision.ESCALATE: "🚨",
            VerdictDecision.CLOSE: "✅",
            VerdictDecision.NEEDS_MORE_INFO: "🔍",
        }
        emoji = decision_emoji.get(self.decision, "❓")

        lines = [
            f"=== VERDICT: {emoji} {self.decision.value.upper()} ===",
            f"Confidence: {self.confidence:.0%}",
            "",
            f"## Threat Assessment",
            f"{self.threat_assessment}",
            "",
            f"Evidence Strength: {self.evidence_strength.value}",
            f"Potential Impact: {self.potential_impact.value}",
            f"Urgency: {self.urgency.value}",
            "",
        ]

        if self.key_evidence:
            lines.append("## Key Evidence")
            for e in self.key_evidence:
                lines.append(f"  ✓ {e}")
            lines.append("")

        if self.gaps_in_evidence:
            lines.append("## Evidence Gaps")
            for g in self.gaps_in_evidence:
                lines.append(f"  ? {g}")
            lines.append("")

        if self.alternative_explanations:
            lines.append("## Alternative Explanations Considered")
            for a in self.alternative_explanations:
                lines.append(f"  - {a}")
            lines.append("")

        if self.assumptions_made:
            lines.append("## Assumptions")
            for a in self.assumptions_made:
                lines.append(f"  * {a}")
            lines.append("")

        lines.append("## Recommendation")
        lines.append(self.recommendation)

        if self.additional_investigation_needed:
            lines.append("")
            lines.append("## Additional Investigation Needed")
            for item in self.additional_investigation_needed:
                lines.append(f"  → {item}")

        return "\n".join(lines)

    def to_hil_summary(self) -> str:
        """Generate a concise summary suitable for human-in-the-loop review.

        Returns:
            Concise summary for human review.
        """
        decision_emoji = {
            VerdictDecision.ESCALATE: "🚨",
            VerdictDecision.CLOSE: "✅",
            VerdictDecision.NEEDS_MORE_INFO: "🔍",
        }
        emoji = decision_emoji.get(self.decision, "❓")

        lines = [
            "=" * 60,
            f"VERDICT: {emoji} {self.decision.value.upper()} (Confidence: {self.confidence:.0%})",
            "=" * 60,
            "",
            f"Impact: {self.potential_impact.value.upper()} | Urgency: {self.urgency.value.upper()}",
            "",
            "THREAT ASSESSMENT:",
            self.threat_assessment,
            "",
            "RECOMMENDATION:",
            self.recommendation,
            "",
        ]

        if self.key_evidence:
            lines.append("KEY EVIDENCE:")
            for e in self.key_evidence[:5]:
                lines.append(f"  • {e}")
            lines.append("")

        if self.alternative_explanations:
            lines.append("ALTERNATIVE EXPLANATIONS:")
            for a in self.alternative_explanations[:3]:
                lines.append(f"  • {a}")
            lines.append("")

        lines.append("=" * 60)
        lines.append("[A]pprove  |  [R]eject  |  [M]ore Info")
        lines.append("=" * 60)

        return "\n".join(lines)
