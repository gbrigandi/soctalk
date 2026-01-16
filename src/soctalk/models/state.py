"""LangGraph state schema for SecOps agent."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Optional, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from soctalk.models.enums import Phase, HumanDecision
from soctalk.models.investigation import Investigation
from soctalk.models.observables import Observable
from soctalk.models.verdict import Verdict


class SupervisorDecision(BaseModel):
    """Decision output from the supervisor node."""

    next_action: str = Field(
        ...,
        description="Next action: ENRICH, INVESTIGATE, VERDICT, CLOSE",
    )
    action_reasoning: str = Field(..., description="Reasoning for the action")
    tp_confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Current confidence this is a true positive (0-1)",
    )
    confidence_reasoning: str = Field(
        default="", description="Reasoning for the confidence level"
    )
    specific_instructions: Optional[str] = Field(
        None, description="Specific instructions for the next worker"
    )


class SecOpsState(BaseModel):
    """State schema for the SecOps LangGraph agent.

    This state is passed between nodes and accumulates investigation data.
    """

    # Current investigation
    investigation: Investigation = Field(
        default_factory=Investigation,
        description="The current investigation being processed",
    )

    # Workflow control
    current_phase: Phase = Field(
        default=Phase.TRIAGE, description="Current investigation phase"
    )

    # Supervisor state
    supervisor_decision: Optional[SupervisorDecision] = Field(
        None, description="Latest decision from supervisor"
    )

    # Enrichment tracking
    pending_observables: list[Observable] = Field(
        default_factory=list, description="Observables not yet enriched"
    )
    current_enrichment_batch: list[Observable] = Field(
        default_factory=list, description="Current batch being enriched"
    )

    # Verdict state
    verdict: Optional[Verdict] = Field(
        None, description="Verdict from reasoning LLM"
    )

    # Human-in-the-loop state
    awaiting_human_approval: bool = Field(
        default=False, description="Whether waiting for human approval"
    )
    human_decision: Optional[HumanDecision] = Field(
        None, description="Decision from human review"
    )
    human_feedback: Optional[str] = Field(
        None, description="Additional feedback from human"
    )

    # Investigation guidance (from verdict if needs more info)
    investigation_guidance: Optional[list[str]] = Field(
        None, description="Guidance for additional investigation"
    )

    # LLM conversation context
    messages: Annotated[Sequence[BaseMessage], add_messages] = Field(
        default_factory=list, description="Conversation messages for LLM context"
    )

    # Error tracking
    last_error: Optional[str] = Field(None, description="Last error encountered")
    error_count: int = Field(default=0, description="Number of errors encountered")

    # Metadata
    iteration_count: int = Field(default=0, description="Number of supervisor iterations")
    started_at: datetime = Field(default_factory=datetime.now)
    last_updated: datetime = Field(default_factory=datetime.now)

    class Config:
        """Pydantic config."""

        arbitrary_types_allowed = True

    def update_timestamp(self) -> None:
        """Update the last_updated timestamp."""
        self.last_updated = datetime.now()

    def increment_iteration(self) -> None:
        """Increment the iteration counter."""
        self.iteration_count += 1
        self.update_timestamp()

    def record_error(self, error: str) -> None:
        """Record an error.

        Args:
            error: Error message.
        """
        self.last_error = error
        self.error_count += 1
        self.update_timestamp()

    def clear_error(self) -> None:
        """Clear the last error."""
        self.last_error = None
        self.update_timestamp()

    def to_context_summary(self) -> str:
        """Generate a summary suitable for LLM context.

        Returns:
            Context summary string.
        """
        lines = [
            "## Current Investigation State",
            "",
            f"**Phase:** {self.current_phase.value}",
            f"**Iteration:** {self.iteration_count}",
            f"**Max Severity:** {self.investigation.max_severity.value}",
            "",
            f"### Alerts ({len(self.investigation.alerts)})",
        ]

        for alert in self.investigation.alerts[:3]:
            lines.append(f"- [{alert.severity.value}] {alert.rule_description[:60]}")

        if len(self.investigation.alerts) > 3:
            lines.append(f"- ... and {len(self.investigation.alerts) - 3} more")

        lines.append("")
        lines.append(
            f"### Observables ({len(self.investigation.enriched_observables)}/"
            f"{len(self.investigation.observables)} enriched)"
        )

        # Show enrichment results
        malicious = self.investigation.malicious_indicators
        suspicious = [
            e for e in self.investigation.suspicious_indicators if e not in malicious
        ]

        if malicious:
            lines.append(f"**Malicious ({len(malicious)}):**")
            for e in malicious[:3]:
                lines.append(f"  🔴 {e.observable.value} ({e.analyzer})")

        if suspicious:
            lines.append(f"**Suspicious ({len(suspicious)}):**")
            for e in suspicious[:3]:
                lines.append(f"  ⚠️ {e.observable.value} ({e.analyzer})")

        pending = len(self.investigation.pending_observables)
        if pending > 0:
            lines.append(f"**Pending enrichment:** {pending} observables")

        if self.investigation.findings:
            lines.append("")
            lines.append(f"### Findings ({len(self.investigation.findings)})")
            for f in self.investigation.findings[:3]:
                lines.append(f"- [{f.severity.value}] {f.description[:60]}")

        if self.supervisor_decision:
            lines.append("")
            lines.append("### Previous Decision")
            lines.append(f"Action: {self.supervisor_decision.next_action}")
            lines.append(
                f"TP Confidence: {self.supervisor_decision.tp_confidence:.0%}"
            )

        if self.last_error:
            lines.append("")
            lines.append(f"### ⚠️ Last Error")
            lines.append(self.last_error)

        return "\n".join(lines)


def create_initial_state(investigation: Investigation) -> dict[str, Any]:
    """Create initial state dictionary for LangGraph.

    Args:
        investigation: The investigation to process.

    Returns:
        State dictionary.
    """
    state = SecOpsState(
        investigation=investigation,
        current_phase=Phase.TRIAGE,
        pending_observables=list(investigation.observables),
    )
    return state.model_dump()
