"""Close investigation node."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from langgraph.config import get_config as get_langgraph_config

from soctalk.models.enums import InvestigationStatus, Phase, HumanDecision, VerdictDecision
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state

logger = structlog.get_logger()


async def close_investigation_node(
    state: dict[str, Any],
) -> dict[str, Any]:
    """Close investigation node.

    Finalizes the investigation with appropriate status and closure reason.

    Args:
        state: Current graph state.

    Returns:
        Updated state with closed investigation.
    """
    try:
        config = get_langgraph_config()
    except RuntimeError:
        config = None

    logger.info("closing_investigation")

    investigation = state.get("investigation", {})
    verdict = state.get("verdict", {})
    human_decision = state.get("human_decision")
    human_feedback = state.get("human_feedback")
    supervisor_decision = state.get("supervisor_decision", {})

    # Determine closure reason and status
    closure_reason = _determine_closure_reason(
        verdict,
        human_decision,
        human_feedback,
        supervisor_decision,
    )

    # Update investigation status
    investigation["status"] = InvestigationStatus.CLOSED.value
    investigation["closed_at"] = datetime.now().isoformat()
    investigation["closure_reason"] = closure_reason

    # Log closure details
    logger.info(
        "investigation_closed",
        investigation_id=investigation.get("id"),
        closure_reason=closure_reason[:100],
        human_decision=human_decision,
        verdict_decision=verdict.get("decision") if verdict else None,
    )

    state["investigation"] = investigation
    state["current_phase"] = Phase.CLOSED.value
    state["last_updated"] = datetime.now().isoformat()

    # Emit investigation closed event
    emitter = get_emitter_from_config(config)
    investigation_id = get_investigation_id_from_state(state)
    if emitter and investigation_id:
        try:
            # Calculate duration
            started_at = state.get("started_at")
            duration_seconds = 0
            if started_at:
                if isinstance(started_at, str):
                    started_at = datetime.fromisoformat(started_at)
                duration_seconds = int((datetime.now() - started_at).total_seconds())

            await emitter.emit_investigation_closed(
                investigation_id=investigation_id,
                status=InvestigationStatus.CLOSED.value,
                resolution=closure_reason[:200],
                verdict_decision=verdict.get("decision") if verdict else None,
                thehive_case_id=investigation.get("thehive_case_id"),
                duration_seconds=duration_seconds,
            )
        except Exception as emit_error:
            logger.warning("event_emission_failed", error=str(emit_error))

    return state


def _determine_closure_reason(
    verdict: dict,
    human_decision: str | None,
    human_feedback: str | None,
    supervisor_decision: dict,
) -> str:
    """Determine the closure reason based on various factors.

    Args:
        verdict: Verdict from reasoning LLM.
        human_decision: Decision from human review.
        human_feedback: Feedback from human review.
        supervisor_decision: Decision from supervisor.

    Returns:
        Closure reason string.
    """
    reasons = []

    # Check human decision
    if human_decision:
        if human_decision == HumanDecision.REJECT.value:
            reasons.append("Rejected by analyst during human review")
            if human_feedback:
                reasons.append(f"Analyst feedback: {human_feedback}")
        elif human_decision == HumanDecision.APPROVE.value:
            reasons.append("Approved by analyst - incident created")
        elif human_decision == HumanDecision.MORE_INFO.value:
            reasons.append("Analyst requested more information but investigation closed")
            if human_feedback:
                reasons.append(f"Analyst feedback: {human_feedback}")

    # Check verdict
    elif verdict:
        verdict_decision = verdict.get("decision")
        if verdict_decision == VerdictDecision.CLOSE.value:
            reasons.append("Closed by AI verdict - likely false positive")
            if verdict.get("recommendation"):
                reasons.append(f"AI recommendation: {verdict['recommendation'][:200]}")
        elif verdict_decision == VerdictDecision.ESCALATE.value:
            reasons.append("Escalation process completed")

    # Check supervisor decision
    elif supervisor_decision:
        action = supervisor_decision.get("next_action")
        if action == "CLOSE":
            reasons.append("Closed by supervisor - insufficient evidence of threat")
            confidence = supervisor_decision.get("tp_confidence", 0)
            reasons.append(f"True positive confidence: {confidence:.0%}")
            if supervisor_decision.get("confidence_reasoning"):
                reasons.append(f"Reasoning: {supervisor_decision['confidence_reasoning'][:200]}")

    # Default reason
    if not reasons:
        reasons.append("Investigation completed - no action required")

    return " | ".join(reasons)


def generate_closure_report(state: dict[str, Any]) -> str:
    """Generate a closure report for the investigation.

    Args:
        state: Final state of the investigation.

    Returns:
        Formatted closure report.
    """
    investigation = state.get("investigation", {})
    verdict = state.get("verdict", {})
    human_decision = state.get("human_decision")

    lines = [
        "=" * 60,
        "INVESTIGATION CLOSURE REPORT",
        "=" * 60,
        "",
        f"Investigation ID: {investigation.get('id', 'unknown')}",
        f"Title: {investigation.get('title', 'Untitled')}",
        f"Status: {investigation.get('status', 'unknown')}",
        f"Closed At: {investigation.get('closed_at', 'unknown')}",
        "",
        "CLOSURE REASON:",
        investigation.get("closure_reason", "No reason provided"),
        "",
    ]

    # Add alert summary
    alerts = investigation.get("alerts", [])
    if alerts:
        lines.append(f"ALERTS ANALYZED: {len(alerts)}")
        for alert in alerts[:5]:
            severity = alert.get("severity", "unknown")
            desc = alert.get("rule_description", "No description")[:50]
            lines.append(f"  - [{severity}] {desc}")
        if len(alerts) > 5:
            lines.append(f"  ... and {len(alerts) - 5} more")
        lines.append("")

    # Add enrichment summary
    enrichments = investigation.get("enrichments", [])
    if enrichments:
        malicious = sum(1 for e in enrichments if e.get("verdict") == "malicious")
        suspicious = sum(1 for e in enrichments if e.get("verdict") == "suspicious")
        clean = sum(1 for e in enrichments if e.get("verdict") == "benign")

        lines.append(f"THREAT INTELLIGENCE: {len(enrichments)} observables enriched")
        lines.append(f"  Malicious: {malicious}")
        lines.append(f"  Suspicious: {suspicious}")
        lines.append(f"  Clean: {clean}")
        lines.append("")

    # Add verdict summary
    if verdict:
        lines.append("AI VERDICT:")
        lines.append(f"  Decision: {verdict.get('decision', 'unknown')}")
        lines.append(f"  Confidence: {verdict.get('confidence', 0):.0%}")
        lines.append(f"  Impact: {verdict.get('potential_impact', 'unknown')}")
        lines.append(f"  Recommendation: {verdict.get('recommendation', 'None')[:100]}")
        lines.append("")

    # Add human decision
    if human_decision:
        lines.append(f"HUMAN DECISION: {human_decision}")
        feedback = state.get("human_feedback")
        if feedback:
            lines.append(f"  Feedback: {feedback}")
        lines.append("")

    # Add TheHive case if created
    case_id = investigation.get("thehive_case_id")
    if case_id:
        lines.append(f"THEHIVE CASE CREATED: {case_id}")
        lines.append("")

    lines.append("=" * 60)

    return "\n".join(lines)
