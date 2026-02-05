"""Verdict node using reasoning LLM for final decision."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_config as get_langgraph_config

from soctalk.config import get_config
from soctalk.llm import create_chat_model
from soctalk.models.enums import (
    Phase,
    VerdictDecision,
    EvidenceStrength,
    ImpactLevel,
    Urgency,
)
from soctalk.models.verdict import Verdict
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state

logger = structlog.get_logger()

VERDICT_SYSTEM_PROMPT = """You are a Principal Security Analyst providing final verdict on a security investigation.

Your role is to critically evaluate all evidence and make a final recommendation before human review.

## Your Task

1. **Evaluate Evidence Quality**: Is the evidence conclusive, circumstantial, or weak?
2. **Consider Alternatives**: Could this be legitimate activity? What would that look like?
3. **Assess Attack Coherence**: If malicious, does the activity tell a coherent attack story?
4. **Identify Gaps**: What evidence is missing that would strengthen/weaken the case?
5. **Risk Calculus**: What's the cost of a false positive vs false negative?

## Challenge Assumptions

- What assumptions are being made?
- Is the confidence level justified by the evidence?
- Are there red flags being overlooked?
- Could benign activities explain these indicators?

## Decision Options

- **ESCALATE**: Evidence supports real threat, send to human for incident creation
- **CLOSE**: Evidence strongly suggests false positive, close investigation
- **NEEDS_MORE_INFO**: Cannot make decision, need specific additional investigation

## Response Format

Provide your verdict as a JSON object with these fields:
- decision: "escalate" | "close" | "needs_more_info"
- confidence: 0.0-1.0
- threat_assessment: Overall assessment of the threat
- evidence_strength: "weak" | "moderate" | "strong" | "conclusive"
- potential_impact: "low" | "medium" | "high" | "critical"
- urgency: "routine" | "elevated" | "urgent" | "immediate"
- key_evidence: List of key evidence points
- gaps_in_evidence: What's missing
- assumptions_made: Assumptions in your analysis
- alternative_explanations: Benign explanations considered
- recommendation: Final recommendation with reasoning
- additional_investigation_needed: (if needs_more_info) What specific investigation is needed
"""

VERDICT_USER_PROMPT_TEMPLATE = """## Investigation Summary

**Investigation ID:** {investigation_id}
**Duration:** {duration}
**Supervisor Iterations:** {iterations}

## Alerts ({alert_count})

{alerts_detail}

## Threat Intelligence Results ({enrichment_count})

{enrichments_detail}

## Findings ({finding_count})

{findings_detail}

## Supervisor's Assessment

**Last Action:** {supervisor_action}
**TP Confidence:** {supervisor_confidence:.0%}
**Reasoning:** {supervisor_reasoning}

---

Provide your final verdict as JSON.
"""


async def verdict_node(
    state: dict[str, Any],
) -> dict[str, Any]:
    """Verdict node - reasoning LLM provides final decision.

    This node uses an advanced reasoning model to:
    1. Critically evaluate all evidence
    2. Challenge assumptions
    3. Make final escalate/close/needs_more_info decision

    Args:
        state: Current graph state.

    Returns:
        Updated state with verdict.
    """
    try:
        config = get_langgraph_config()
    except RuntimeError:
        config = None

    logger.info("verdict_node_started")

    app_config = get_config()

    try:
        # Build comprehensive context
        context = _build_verdict_context(state)

        # Get verdict from reasoning LLM
        verdict = await _get_verdict(app_config, context)

        state["verdict"] = verdict.model_dump()
        state["current_phase"] = Phase.VERDICT.value

        # Track retry count for NEEDS_MORE_INFO decisions
        if verdict.decision == VerdictDecision.NEEDS_MORE_INFO:
            state["verdict_retry_count"] = state.get("verdict_retry_count", 0) + 1
            logger.info(
                "verdict_needs_more_info",
                retry_count=state["verdict_retry_count"],
            )

        logger.info(
            "verdict_rendered",
            decision=verdict.decision.value,
            confidence=verdict.confidence,
            impact=verdict.potential_impact.value,
        )

        # Emit verdict event
        emitter = get_emitter_from_config(config)
        investigation_id = get_investigation_id_from_state(state)
        if emitter and investigation_id:
            try:
                await emitter.emit_verdict_rendered(
                    investigation_id=investigation_id,
                    decision=verdict.decision.value,
                    confidence=verdict.confidence,
                    reasoning=verdict.recommendation,
                    threat_actor=None,  # Could extract from verdict if available
                )
            except Exception as emit_error:
                logger.warning("event_emission_failed", error=str(emit_error))

    except Exception as e:
        logger.error("verdict_node_error", error=str(e))

        # Create a default verdict on error
        state["verdict"] = Verdict(
            decision=VerdictDecision.NEEDS_MORE_INFO,
            confidence=0.5,
            threat_assessment="Unable to complete assessment due to error",
            evidence_strength=EvidenceStrength.WEAK,
            potential_impact=ImpactLevel.MEDIUM,
            urgency=Urgency.ROUTINE,
            key_evidence=[],
            gaps_in_evidence=["Verdict assessment failed"],
            assumptions_made=[],
            alternative_explanations=[],
            recommendation=f"Error during verdict: {str(e)}. Manual review required.",
            additional_investigation_needed=["Manual review required due to system error"],
            reasoning_model=app_config.llm.reasoning_model,
        ).model_dump()

        # Track retry count for error-induced NEEDS_MORE_INFO
        state["verdict_retry_count"] = state.get("verdict_retry_count", 0) + 1
        state["last_error"] = str(e)

    state["last_updated"] = datetime.now().isoformat()
    return state


def _build_verdict_context(state: dict[str, Any]) -> dict[str, Any]:
    """Build comprehensive context for verdict LLM.

    Args:
        state: Current state.

    Returns:
        Context dictionary for prompt formatting.
    """
    investigation = state.get("investigation", {})
    alerts = investigation.get("alerts", [])
    enrichments = investigation.get("enrichments", [])
    findings = investigation.get("findings", [])
    supervisor_decision = state.get("supervisor_decision", {})

    # Format alerts
    alerts_lines = []
    for alert in alerts:
        severity = alert.get("severity", "unknown")
        desc = alert.get("rule_description", "No description")
        agent = alert.get("source", {}).get("agent_name", "unknown")
        level = alert.get("level", 0)
        timestamp = alert.get("timestamp", "unknown")

        alerts_lines.append(f"### [{severity.upper()}] Level {level}")
        alerts_lines.append(f"**Description:** {desc}")
        alerts_lines.append(f"**Agent:** {agent}")
        alerts_lines.append(f"**Time:** {timestamp}")
        alerts_lines.append("")

    # Format enrichments
    enrichments_lines = []
    malicious_count = 0
    suspicious_count = 0

    for e in enrichments:
        verdict_val = e.get("verdict", "unknown")
        obs = e.get("observable", {})
        value = obs.get("value", "unknown")
        obs_type = obs.get("type", "unknown")
        analyzer = e.get("analyzer", "unknown")
        confidence = e.get("confidence", 0)

        if verdict_val == "malicious":
            malicious_count += 1
            emoji = "🔴"
        elif verdict_val == "suspicious":
            suspicious_count += 1
            emoji = "⚠️"
        elif verdict_val == "benign":
            emoji = "✅"
        else:
            emoji = "❓"

        enrichments_lines.append(
            f"{emoji} **{obs_type}:** {value}\n"
            f"   Analyzer: {analyzer} | Verdict: {verdict_val} | Confidence: {confidence:.0%}"
        )

    enrichments_lines.insert(0, f"**Summary:** {malicious_count} malicious, {suspicious_count} suspicious\n")

    # Format findings
    findings_lines = []
    for f in findings:
        severity = f.get("severity", "unknown")
        desc = f.get("description", "No description")
        evidence = f.get("evidence", [])

        findings_lines.append(f"### [{severity.upper()}] {desc}")
        if evidence:
            findings_lines.append("Evidence:")
            for ev in evidence[:3]:
                findings_lines.append(f"  - {ev}")
        findings_lines.append("")

    # Calculate duration
    started_at = state.get("started_at")
    if started_at:
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        duration = datetime.now() - started_at
        duration_str = f"{duration.total_seconds():.0f} seconds"
    else:
        duration_str = "unknown"

    return {
        "investigation_id": investigation.get("id", "unknown"),
        "duration": duration_str,
        "iterations": state.get("iteration_count", 0),
        "alert_count": len(alerts),
        "alerts_detail": "\n".join(alerts_lines) if alerts_lines else "No alerts",
        "enrichment_count": len(enrichments),
        "enrichments_detail": "\n".join(enrichments_lines) if enrichments_lines else "No enrichments",
        "finding_count": len(findings),
        "findings_detail": "\n".join(findings_lines) if findings_lines else "No findings",
        "supervisor_action": supervisor_decision.get("next_action", "unknown"),
        "supervisor_confidence": supervisor_decision.get("tp_confidence", 0.5),
        "supervisor_reasoning": supervisor_decision.get("confidence_reasoning", "No reasoning"),
    }


async def _get_verdict(config: Any, context: dict[str, Any]) -> Verdict:
    """Get verdict from reasoning LLM.

    Args:
        config: Application configuration.
        context: Context dictionary.

    Returns:
        Verdict object.
    """
    # Use reasoning model (more capable)
    llm = create_chat_model(
        config.llm,
        model=config.llm.reasoning_model,
        temperature=0.1,  # Low temperature for more consistent reasoning
        max_tokens=2048,
    )

    messages = [
        SystemMessage(content=VERDICT_SYSTEM_PROMPT),
        HumanMessage(content=VERDICT_USER_PROMPT_TEMPLATE.format(**context)),
    ]

    response = await llm.ainvoke(messages)
    response_text = response.content

    # Parse verdict
    verdict_data = _parse_verdict_response(response_text)

    # Safely parse enum values with fallbacks
    def safe_enum(enum_class, value, default):
        try:
            return enum_class(value)
        except (ValueError, KeyError):
            return default

    # Normalize list fields - LLM sometimes returns strings instead of lists
    def ensure_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return value
        return []

    additional_inv = verdict_data.get("additional_investigation_needed")
    if additional_inv is not None:
        additional_inv = ensure_list(additional_inv) or None

    return Verdict(
        decision=safe_enum(VerdictDecision, verdict_data.get("decision", "needs_more_info"), VerdictDecision.NEEDS_MORE_INFO),
        confidence=float(verdict_data.get("confidence", 0.5)),
        threat_assessment=verdict_data.get("threat_assessment", "No assessment provided"),
        evidence_strength=safe_enum(EvidenceStrength, verdict_data.get("evidence_strength", "weak"), EvidenceStrength.WEAK),
        potential_impact=safe_enum(ImpactLevel, verdict_data.get("potential_impact", "medium"), ImpactLevel.MEDIUM),
        urgency=safe_enum(Urgency, verdict_data.get("urgency", "routine"), Urgency.ROUTINE),
        key_evidence=ensure_list(verdict_data.get("key_evidence")),
        gaps_in_evidence=ensure_list(verdict_data.get("gaps_in_evidence")),
        assumptions_made=ensure_list(verdict_data.get("assumptions_made")),
        alternative_explanations=ensure_list(verdict_data.get("alternative_explanations")),
        recommendation=verdict_data.get("recommendation", "No recommendation provided"),
        additional_investigation_needed=additional_inv,
        reasoning_model=config.llm.reasoning_model,
    )


def _parse_verdict_response(response_text: str) -> dict[str, Any]:
    """Parse verdict response from LLM.

    Args:
        response_text: Raw LLM response.

    Returns:
        Parsed verdict dictionary.
    """
    import re

    # Try to find JSON block
    json_match = re.search(r"```json\s*(.*?)\s*```", response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object (more permissive)
    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # Fallback: try entire response
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Last resort: extract what we can
    result = {
        "decision": "needs_more_info",
        "confidence": 0.5,
        "threat_assessment": "Unable to parse verdict response",
        "evidence_strength": "weak",
        "potential_impact": "medium",
        "urgency": "routine",
        "key_evidence": [],
        "gaps_in_evidence": ["Failed to parse LLM response"],
        "assumptions_made": [],
        "alternative_explanations": [],
        "recommendation": "Manual review required - verdict parsing failed",
    }

    # Try to extract decision
    response_lower = response_text.lower()
    if "escalate" in response_lower:
        result["decision"] = "escalate"
    elif "close" in response_lower and "false positive" in response_lower:
        result["decision"] = "close"

    return result
