"""Verdict node using reasoning LLM for final decision."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from langchain_core.messages import HumanMessage

from soctalk.config import get_config
from soctalk.inference import (
    InferenceAccounting,
    InferenceRequest,
    InferenceTier,
    ainvoke_request,
    resolve_tier_sampling,
)
from soctalk.llm import classify_llm_error as _classify_llm_error
from soctalk.authorization.render import verdict_authorization_detail
from soctalk.models.enums import Phase, VerdictDecision
from soctalk.models.verdict import Verdict, VerdictDraft

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

## Authorization Reasoning (when an Authorization Context section is present)

Decide whether the activity was AUTHORIZED, not just whether it looks unusual. Close requires
ALL FOUR to hold: (1) sanctioned-or-routine — an approving record of the right kind (change
ticket, standing baseline, or established routine history) names this activity; (2) in-scope —
a SINGLE record fully covers it: right subject, target, action, time window, calendar validity,
CAB approval if required, not blocked by an active freeze. Never combine two partial records.
Expired, pending, future-effective, unapproved-CAB, out-of-window, wrong-host/account/path
records do NOT cover, no matter how official they look; (3) actor/target genuine — not
compromised or contained, no service account used interactively, no off-call privileged human;
(4) policy-allowed — no high-priority policy forbids it without a waiver or a covering
break-glass emergency change.

Distinguish ABSENT evidence from CONTRADICTED evidence — they call for different decisions:
- ABSENT: the context carries no authorization records at all for this activity. Never treat
  absence as implicit approval; when the case hinges on authorization and evidence is genuinely
  missing, prefer needs_more_info over close.
- CONTRADICTED: authorization records ARE present but fail to cover — expired, pending,
  future-effective, CAB-unapproved, out-of-window, frozen, or scoped to a different
  host/account/path. That mismatch is itself the finding: someone acted outside the terms of
  their authorization. ESCALATE. Do not choose needs_more_info to "verify the discrepancy" —
  the discrepancy is the signal, and a human incident responder is the right verifier.

Authorization evidence lowers suspicion; it NEVER overrides malicious indicators, IOC matches,
or active-incident correlation.

## Decision Options

- **ESCALATE**: Evidence supports real threat, send to human for incident creation
- **CLOSE**: Evidence strongly suggests false positive, close investigation
- **NEEDS_MORE_INFO**: Cannot make decision, need specific additional investigation

## Response Format

Provide your verdict with these fields:
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

# Ordered most-static -> most-variable: alert evidence first, per-run
# metadata (ID, wall-clock duration, iteration count) at the tail so the
# prompt shares the longest possible cacheable prefix with the supervisor
# calls that preceded it in the same investigation.
VERDICT_USER_PROMPT_TEMPLATE = """## Alerts ({alert_count})

{alerts_detail}

## Threat Intelligence Results ({enrichment_count})

{enrichments_detail}

## Findings ({finding_count})

{findings_detail}

{authorization_detail}## Supervisor's Assessment

**Last Action:** {supervisor_action}
**TP Confidence:** {supervisor_confidence:.0%}
**Reasoning:** {supervisor_reasoning}

## Run Metadata

**Investigation ID:** {investigation_id}
**Duration:** {duration}
**Supervisor Iterations:** {iterations}

---

Provide your final verdict.
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
    logger.info("verdict_node_started")

    app_config = get_config()

    try:
        # Build comprehensive context
        context = _build_verdict_context(state)

        # Get verdict from reasoning LLM
        verdict = await _get_verdict(app_config, context, state)

        state["verdict"] = verdict.model_dump()
        state["current_phase"] = Phase.VERDICT.value

        from soctalk.core.ir import replay_events
        from soctalk.graph.event_sink import emit as emit_replay

        emit_replay(replay_events.verdict_rendered(state["verdict"]))

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

    except Exception as e:
        # Classify so the worker can route LLM-provider failures
        # (credit lack, rate limit, transient 5xx) to ``failed`` status
        # rather than a fake escalated verdict — those errors carry the
        # raw API response string which would otherwise become the
        # user-facing HIL review description (real incident from
        # 2026-05). Anthropic / OpenAI errors all hand us a
        # ``status_code`` attribute via the langchain wrapper.
        category = _classify_llm_error(e)
        logger.error(
            "verdict_node_error",
            error=str(e)[:200],
            category=category,
        )
        state["verdict_error"] = {
            "category": category,
            # Full string kept in state for operator debugging in logs
            # only — the worker MUST NOT propagate this into any
            # user-facing field (verdict_summary, pending_reviews.desc).
            "message": str(e)[:500],
        }
        state["last_error"] = f"verdict_failed:{category}"
        # Track retry so the supervisor's max_retries gate fires; the
        # graph keeps running for transient categories but ``verdict``
        # is NOT populated — the worker treats missing verdict as a
        # failed run.
        state["verdict_retry_count"] = state.get("verdict_retry_count", 0) + 1

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

    # Format alerts. Cap the render (issue #26 correlation can put many
    # alerts on one investigation) so a large correlated group doesn't blow
    # up the verdict prompt; alerts arrive severity-ordered so the cap keeps
    # the most severe, with an explicit overflow marker.
    _VERDICT_ALERT_CAP = 10
    alerts_lines = []
    for alert in alerts[:_VERDICT_ALERT_CAP]:
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
    if len(alerts) > _VERDICT_ALERT_CAP:
        alerts_lines.append(
            f"... and {len(alerts) - _VERDICT_ALERT_CAP} more correlated alerts "
            f"(showing the {_VERDICT_ALERT_CAP} most severe)"
        )
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
        now = (
            datetime.now(started_at.tzinfo)
            if started_at.tzinfo is not None
            else datetime.now()
        )
        duration = now - started_at
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
        "authorization_detail": verdict_authorization_detail(investigation),
        "supervisor_action": supervisor_decision.get("next_action", "unknown"),
        "supervisor_confidence": supervisor_decision.get("tp_confidence", 0.5),
        "supervisor_reasoning": supervisor_decision.get("confidence_reasoning", "No reasoning"),
    }


async def _get_verdict(
    config: Any,
    context: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> Verdict:
    """Get verdict from reasoning LLM.

    Args:
        config: Application configuration.
        context: Context dictionary.

    Returns:
        Verdict object.
    """
    # Reasoning tier (more capable) via the single ainvoke_request seam (#32).
    req = InferenceRequest(
        tier=InferenceTier.REASONING,
        metadata=InferenceAccounting(producer="supervisor.verdict", budget_state=state),
        output_schema=VerdictDraft,
        system=VERDICT_SYSTEM_PROMPT,
        messages=[HumanMessage(content=VERDICT_USER_PROMPT_TEMPLATE.format(**context))],
        # Reasoning sampling: a per-tier override (SOCTALK_REASONING_TEMPERATURE
        # / _MAX_TOKENS) wins; otherwise the verdict's tuned defaults (slightly
        # warmer than the router, longer output for the rationale).
        sampling=resolve_tier_sampling(
            config.llm, InferenceTier.REASONING, temperature=0.1, max_tokens=2048,
        ),
    )
    res = await ainvoke_request(req, cfg=config.llm)
    draft = res.parsed
    return Verdict(
        **draft.model_dump(),
        reasoning_model=res.resolved.model,
    )


