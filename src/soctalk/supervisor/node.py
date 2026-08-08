"""Supervisor node implementation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from langchain_core.messages import HumanMessage

from soctalk.authorization.render import supervisor_authorization_lines
from soctalk.config import get_config
from soctalk.core.ir import replay_events
from soctalk.graph import budget as token_budget
from soctalk.graph.event_sink import emit as emit_replay
from soctalk.inference import (
    InferenceAccounting,
    InferenceRequest,
    InferenceTier,
    ainvoke_request,
    resolve_tier_sampling,
)
from soctalk.llm import classify_llm_error
from soctalk.models.enums import Phase
from soctalk.models.state import SupervisorDecision
from soctalk.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT, SUPERVISOR_USER_PROMPT_TEMPLATE
from soctalk.triage_policy.gate import legal_actions_for

logger = structlog.get_logger()

# Maximum iterations before forcing verdict
MAX_ITERATIONS = 10

# Narrowed structured-output schemas per legal-action set (#45): when the active
# triage policy restricts the supervisor's legal actions, the restriction is applied
# BEFORE the call — an illegal action cannot even be sampled. Cached because the
# handful of distinct legal sets is tiny and model classes are heavy to build.
_CONSTRAINED_SCHEMAS: dict[frozenset[str], type[SupervisorDecision]] = {}


def constrained_decision_schema(legal: frozenset[str]) -> type[SupervisorDecision]:
    """A SupervisorDecision subclass whose ``next_action`` accepts only the legal
    set. String Literal values keep ``model_dump()`` output identical to the base
    schema's (its config already dumps enum values as plain strings)."""
    cached = _CONSTRAINED_SCHEMAS.get(legal)
    if cached is not None:
        return cached
    from typing import Literal

    from pydantic import Field, create_model

    ordered = tuple(sorted(legal))
    model = create_model(
        "ConstrainedSupervisorDecision",
        __base__=SupervisorDecision,
        next_action=(
            Literal[ordered],  # type: ignore[valid-type]
            Field(..., description=f"Next action: {', '.join(ordered)}"),
        ),
    )
    _CONSTRAINED_SCHEMAS[legal] = model
    return model


async def supervisor_node(
    state: dict[str, Any],
) -> dict[str, Any]:
    """Supervisor node - orchestrates the investigation workflow.

    The supervisor:
    1. Analyzes current investigation state
    2. Assesses TP confidence
    3. Decides next action (ENRICH, INVESTIGATE, VERDICT, CLOSE)

    Args:
        state: Current graph state.

    Returns:
        Updated state with supervisor decision.
    """
    logger.info("supervisor_started", iteration=state.get("iteration_count", 0))

    app_config = get_config()

    token_budget.ensure(state)
    # Soft warning at the configured ratio (75% default, #103): surface an
    # approaching budget once, before the hard halt below. Does not stop the run.
    if not state.get("budget_warned") and token_budget.crossed_soft_warn(state):
        state["budget_warned"] = True
        _ratio = token_budget.soft_warn_ratio()
        logger.warning(
            "case_run_budget_warning",
            tokens_used=state["tokens_used"],
            tokens_budget=state["tokens_budget"],
            dollars_used=round(state["dollars_used"], 4),
            dollars_budget=state["dollars_budget"],
            ratio=_ratio,
        )
        emit_replay(
            replay_events.budget_warning(
                tokens_used=state["tokens_used"],
                tokens_budget=state["tokens_budget"],
                dollars_used=state["dollars_used"],
                dollars_budget=state["dollars_budget"],
                ratio=_ratio,
            )
        )
    if token_budget.over_budget(state):
        cap_reason = token_budget.reason(state)
        logger.warning(
            "case_run_budget_exceeded",
            tokens_used=state["tokens_used"],
            tokens_budget=state["tokens_budget"],
            dollars_used=round(state["dollars_used"], 4),
            dollars_budget=state["dollars_budget"],
            reason=cap_reason,
        )
        state["supervisor_decision"] = SupervisorDecision(
            next_action="CLOSE",
            action_reasoning=f"budget_exceeded: {cap_reason}",
            tp_confidence=0.0,
            confidence_reasoning="case_run terminated by per-run cost cap",
            specific_instructions=None,
        ).model_dump()
        state["budget_terminated"] = True
        emit_replay(
            replay_events.supervisor_decision(
                state["supervisor_decision"],
                iteration=state.get("iteration_count", 0),
            )
        )
        return state

    # Increment iteration counter
    iteration = state.get("iteration_count", 0) + 1
    state["iteration_count"] = iteration

    # Check for max iterations
    if iteration >= MAX_ITERATIONS:
        logger.warning("max_iterations_reached", iteration=iteration)
        state["supervisor_decision"] = SupervisorDecision(
            next_action="VERDICT",
            action_reasoning="Maximum iterations reached - forcing verdict",
            tp_confidence=0.5,
            confidence_reasoning="Unable to reach conclusion within iteration limit",
        ).model_dump()
        state["current_phase"] = Phase.VERDICT.value
        emit_replay(
            replay_events.supervisor_decision(
                state["supervisor_decision"], iteration=iteration
            )
        )
        return state

    # Build context summary
    context_summary = _build_context_summary(state)

    # Call LLM for decision
    try:
        decision = await _get_supervisor_decision(app_config, context_summary, state)
        state["supervisor_decision"] = decision.model_dump()

        # Update phase based on decision
        if decision.next_action == "VERDICT":
            state["current_phase"] = Phase.VERDICT.value
        elif decision.next_action == "CLOSE":
            state["current_phase"] = Phase.CLOSED.value
        elif decision.next_action == "ENRICH":
            state["current_phase"] = Phase.ENRICHMENT.value
        elif decision.next_action == "CONTEXTUALIZE":
            state["current_phase"] = Phase.ENRICHMENT.value
        elif decision.next_action == "INVESTIGATE":
            state["current_phase"] = Phase.ANALYSIS.value

        logger.info(
            "supervisor_decision",
            action=decision.next_action,
            confidence=decision.tp_confidence,
            reasoning=decision.action_reasoning[:100],
        )
        emit_replay(
            replay_events.supervisor_decision(
                state["supervisor_decision"], iteration=iteration
            )
        )

    except Exception as e:
        # Never fabricate a triage decision from an error. Classify so the
        # worker can route provider failures (credit lack, rate limit,
        # timeout, schema validation) to run status ``failed`` instead of
        # a fake enrichment loop or verdict; the graph routes straight to
        # close_investigation without HIL.
        category = classify_llm_error(e)
        logger.error("supervisor_error", category=category, error=str(e))
        state["supervisor_error"] = {"category": category}
        state["last_error"] = f"supervisor_failed:{category}"

    state["last_updated"] = datetime.now().isoformat()
    return state


def _build_context_summary(state: dict[str, Any]) -> str:
    """Build a context summary for the supervisor.

    Args:
        state: Current state.

    Returns:
        Context summary string.
    """
    investigation = state.get("investigation", {})
    alerts = investigation.get("alerts", [])
    enrichments = investigation.get("enrichments", [])
    findings = investigation.get("findings", [])
    pending = state.get("pending_observables", [])
    misp_context = investigation.get("misp_context", {})

    # Cache-stability: volatile fields (iteration, phase, errors) render at
    # the TAIL — everything before them is stable or append-only across the
    # supervisor loop, so the prompt prefix stays byte-identical.
    lines = [
        f"### Alerts ({len(alerts)})",
    ]

    # Summarize alerts
    for alert in alerts[:5]:
        severity = alert.get("severity", "unknown")
        desc = alert.get("rule_description", "No description")[:60]
        agent = alert.get("source", {}).get("agent_name", "unknown")
        lines.append(f"- [{severity}] {desc} (agent: {agent})")
        # Rule semantics (issue #17 T6): show MITRE techniques/tactics and
        # rule groups when the evidence store carried them.
        mitre = alert.get("mitre") or {}
        techniques = mitre.get("techniques") or mitre.get("ids") or []
        if techniques:
            lines.append(f"  MITRE: {', '.join(str(t) for t in techniques[:6])}")
        groups = alert.get("rule_groups") or []
        if groups:
            lines.append(f"  Rule groups: {', '.join(str(g) for g in groups[:6])}")

    if len(alerts) > 5:
        lines.append(f"- ... and {len(alerts) - 5} more alerts")

    # Observables status. Fall back to enriched+pending when the raw observables
    # list is not carried (e.g. state assembled from enrichments only), so the
    # ratio never reads as a nonsensical "1/0 enriched".
    enriched_count = len(enrichments)
    pending_count = len(pending)
    total_obs = max(len(investigation.get("observables", [])), enriched_count + pending_count)

    lines.append("")
    lines.append(f"### Observables ({enriched_count}/{total_obs} enriched, {pending_count} pending)")

    # Enrichment results
    malicious = []
    suspicious = []
    clean = []

    for e in enrichments:
        verdict = e.get("verdict", "unknown")
        obs = e.get("observable", {})
        value = obs.get("value", "unknown")[:30]
        obs_type = obs.get("type", "unknown")
        analyzer = e.get("analyzer", "unknown")

        entry = f"{obs_type}: {value} ({analyzer})"
        if e.get("synthetic"):
            entry = f"[SYNTHETIC DEMO DATA] {entry}"

        if verdict == "malicious":
            malicious.append(entry)
        elif verdict == "suspicious":
            suspicious.append(entry)
        elif verdict == "benign":
            clean.append(entry)

    if malicious:
        lines.append(f"**🔴 Malicious ({len(malicious)}):**")
        for m in malicious[:3]:
            lines.append(f"  - {m}")
        if len(malicious) > 3:
            lines.append(f"  - ... and {len(malicious) - 3} more")

    if suspicious:
        lines.append(f"**⚠️ Suspicious ({len(suspicious)}):**")
        for s in suspicious[:3]:
            lines.append(f"  - {s}")

    if clean:
        lines.append(f"**✅ Clean ({len(clean)}):** {len(clean)} observables")

    # Pending observables
    if pending:
        lines.append("")
        lines.append(f"**Pending enrichment ({len(pending)}):**")
        for p in pending[:5]:
            if isinstance(p, dict):
                lines.append(f"  - {p.get('type', 'unknown')}: {p.get('value', 'unknown')[:30]}")
            else:
                lines.append(f"  - {p}")

    # Findings
    if findings:
        lines.append("")
        lines.append(f"### Findings ({len(findings)})")
        for f in findings[:3]:
            severity = f.get("severity", "unknown")
            desc = f.get("description", "No description")[:60]
            prefix = "[SYNTHETIC DEMO DATA] " if f.get("synthetic") else ""
            lines.append(f"- {prefix}[{severity}] {desc}")

    # MISP Threat Intelligence Context
    if misp_context:
        lines.append("")
        lines.append("### MISP Threat Intelligence")
        if misp_context.get("synthetic"):
            lines.append(
                "**[SYNTHETIC DEMO DATA]** This threat intelligence was generated for "
                "demonstration. It is not the result of any external lookup. Do not treat "
                "it as verified evidence of malice."
            )

        misp_matches = misp_context.get("matches", [])
        threat_actors = misp_context.get("threat_actors", [])
        campaigns = misp_context.get("campaigns", [])
        warninglist_hits = misp_context.get("warninglist_hits", [])
        checked_iocs = misp_context.get("checked_iocs", [])

        lines.append(f"**IOCs checked:** {len(checked_iocs)}, **Matches:** {len(misp_matches)}")

        if misp_matches:
            lines.append(f"**🎯 MISP IOC Matches ({len(misp_matches)}):**")
            for m in misp_matches[:3]:
                to_ids = "IDS" if m.get("to_ids") else ""
                events = ", ".join(m.get("event_ids", [])[:2])
                lines.append(f"  - {m.get('value', 'unknown')[:30]} ({m.get('type', '')}) {to_ids} [Events: {events}]")

        if threat_actors:
            lines.append(f"**🕵️ Threat Actors:** {', '.join(threat_actors[:3])}")

        if campaigns:
            lines.append(f"**📋 Campaigns:** {', '.join(campaigns[:3])}")

        if warninglist_hits:
            lines.append(f"**⚠️ Warninglist hits (potential FPs):** {len(warninglist_hits)}")
    else:
        # MISP not yet checked. Surface a hint so the router can see CONTEXTUALIZE
        # is a still-undone step, but tailor the wording to the enrichment state so
        # it never contradicts a pending-enrichment case:
        #   - enrichment DONE (>=1 enriched, 0 pending) -> CONTEXTUALIZE is next
        #   - enrichment still PENDING -> neutral note (ENRICH comes first)
        # Only show it when there is observable activity to attribute at all.
        if enriched_count > 0 and pending_count == 0:
            lines.append("")
            lines.append("### MISP Threat Intelligence")
            lines.append("**Not yet checked** - enrichment is done; CONTEXTUALIZE for threat attribution is the next gathering step")
        elif enriched_count > 0 or total_obs > 0:
            lines.append("")
            lines.append("### MISP Threat Intelligence")
            lines.append("**Not yet checked** - retrieve after enrichment completes (CONTEXTUALIZE)")

    # Authorization context (epic M1): stable within a run, so it stays ahead of the
    # volatile tail; renders nothing when the investigation carries no authorization key.
    lines.extend(supervisor_authorization_lines(investigation))

    # Previous decision
    prev_decision = state.get("supervisor_decision")
    if prev_decision:
        lines.append("")
        lines.append("### Previous Decision")
        lines.append(f"Action: {prev_decision.get('next_action', 'unknown')}")
        lines.append(f"TP Confidence: {prev_decision.get('tp_confidence', 0):.0%}")

    # Errors
    last_error = state.get("last_error")
    if last_error:
        lines.append("")
        lines.append("### ⚠️ Last Error")
        lines.append(last_error[:200])

    # Volatile tail (see note at top of this function).
    lines.append("")
    lines.append(f"**Iteration:** {state.get('iteration_count', 0)}")
    lines.append(f"**Phase:** {state.get('current_phase', 'unknown')}")

    return "\n".join(lines)


async def _get_supervisor_decision(
    config: Any,
    context_summary: str,
    state: dict[str, Any] | None = None,
) -> SupervisorDecision:
    """Get a schema-enforced decision from the router LLM.

    Issued as an ``InferenceRequest`` on the ROUTER tier through the single
    ``ainvoke_request`` seam (#32): structured output (tool-use on Anthropic,
    json_schema on OpenAI) replaces the old free-text-JSON parsing — an invalid
    ``next_action`` or malformed response is retried once with the validation
    error fed back, then fails the run loudly via SchemaValidationError. Every
    raw response is funnelled through budget.track by the dispatcher.
    """
    # Pre-call legal-action narrowing (#45): the triage policy's legal set for the
    # current phase becomes the schema's action enum. The routing gate re-checks
    # after the call (defense in depth for state-written decisions).
    schema: type[SupervisorDecision] = SupervisorDecision
    if state is not None:
        legal = legal_actions_for(state)
        if legal is not None:
            schema = constrained_decision_schema(legal)

    req = InferenceRequest(
        tier=InferenceTier.ROUTER,
        metadata=InferenceAccounting(producer="supervisor.router", budget_state=state),
        output_schema=schema,
        system=SUPERVISOR_SYSTEM_PROMPT,
        messages=[HumanMessage(
            content=SUPERVISOR_USER_PROMPT_TEMPLATE.format(context_summary=context_summary)
        )],
        # Router sampling: a per-tier override (SOCTALK_FAST_TEMPERATURE /
        # _MAX_TOKENS) wins; otherwise the tenant-global default. A schema-
        # constrained router decision is tiny, so the token cap is a ceiling not
        # a target — tuning it changes cost/latency, not routing behaviour.
        sampling=resolve_tier_sampling(
            config.llm, InferenceTier.ROUTER,
            temperature=config.llm.temperature, max_tokens=config.llm.max_tokens,
        ),
    )
    res = await ainvoke_request(req, cfg=config.llm)
    return res.parsed
