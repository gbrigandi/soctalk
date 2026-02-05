"""Supervisor node implementation."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from soctalk.config import get_config
from soctalk.llm import create_chat_model
from soctalk.models.enums import Phase
from soctalk.models.state import SecOpsState, SupervisorDecision
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state
from soctalk.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT, SUPERVISOR_USER_PROMPT_TEMPLATE

logger = structlog.get_logger()

# Maximum iterations before forcing verdict
MAX_ITERATIONS = 10


async def supervisor_node(
    state: dict[str, Any],
    run_config: RunnableConfig | None = None,
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
        return state

    # Build context summary
    context_summary = _build_context_summary(state)

    # Call LLM for decision
    try:
        decision = await _get_supervisor_decision(app_config, context_summary)
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

        # Emit supervisor decision event
        emitter = get_emitter_from_config(run_config)
        investigation_id = get_investigation_id_from_state(state)
        if emitter and investigation_id:
            try:
                await emitter.emit_supervisor_decision(
                    investigation_id=investigation_id,
                    action=decision.next_action,
                    reasoning=decision.action_reasoning,
                    tp_confidence=decision.tp_confidence,
                    iteration=iteration,
                )
            except Exception as emit_error:
                logger.warning("event_emission_failed", error=str(emit_error))

    except Exception as e:
        logger.error("supervisor_error", error=str(e))
        # Default to enrichment on error if there are pending observables
        pending = state.get("pending_observables", [])
        if pending:
            state["supervisor_decision"] = SupervisorDecision(
                next_action="ENRICH",
                action_reasoning=f"Error in decision making, defaulting to enrichment: {str(e)}",
                tp_confidence=0.5,
                confidence_reasoning="Unable to assess due to error",
            ).model_dump()
        else:
            state["supervisor_decision"] = SupervisorDecision(
                next_action="VERDICT",
                action_reasoning=f"Error in decision making, proceeding to verdict: {str(e)}",
                tp_confidence=0.5,
                confidence_reasoning="Unable to assess due to error",
            ).model_dump()
            state["current_phase"] = Phase.VERDICT.value

        state["last_error"] = str(e)

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

    lines = [
        f"**Iteration:** {state.get('iteration_count', 0)}",
        f"**Phase:** {state.get('current_phase', 'unknown')}",
        "",
        f"### Alerts ({len(alerts)})",
    ]

    # Summarize alerts
    for alert in alerts[:5]:
        severity = alert.get("severity", "unknown")
        desc = alert.get("rule_description", "No description")[:60]
        agent = alert.get("source", {}).get("agent_name", "unknown")
        lines.append(f"- [{severity}] {desc} (agent: {agent})")

    if len(alerts) > 5:
        lines.append(f"- ... and {len(alerts) - 5} more alerts")

    # Observables status
    total_obs = len(investigation.get("observables", []))
    enriched_count = len(enrichments)
    pending_count = len(pending)

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
            lines.append(f"- [{severity}] {desc}")

    # MISP Threat Intelligence Context
    if misp_context:
        lines.append("")
        lines.append("### MISP Threat Intelligence")

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
        # MISP not yet checked
        total_obs = len(investigation.get("observables", []))
        if total_obs > 0:
            lines.append("")
            lines.append("### MISP Threat Intelligence")
            lines.append("**Not yet checked** - consider CONTEXTUALIZE action for threat attribution")

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
        lines.append(f"### ⚠️ Last Error")
        lines.append(last_error[:200])

    return "\n".join(lines)


async def _get_supervisor_decision(config: Any, context_summary: str) -> SupervisorDecision:
    """Get decision from LLM.

    Args:
        config: Application configuration.
        context_summary: Context summary for the LLM.

    Returns:
        SupervisorDecision object.
    """
    llm = create_chat_model(
        config.llm,
        model=config.llm.fast_model,
        temperature=config.llm.temperature,
        max_tokens=1024,
    )

    messages = [
        SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
        HumanMessage(content=SUPERVISOR_USER_PROMPT_TEMPLATE.format(context_summary=context_summary)),
    ]

    response = await llm.ainvoke(messages)
    response_text = response.content

    # Parse JSON response
    decision_data = _parse_decision_response(response_text)

    return SupervisorDecision(
        next_action=decision_data.get("next_action", "ENRICH"),
        action_reasoning=decision_data.get("action_reasoning", "No reasoning provided"),
        tp_confidence=float(decision_data.get("tp_confidence", 0.5)),
        confidence_reasoning=decision_data.get("confidence_reasoning", "No reasoning provided"),
        specific_instructions=decision_data.get("specific_instructions"),
    )


def _sanitize_json_string(json_str: str) -> str:
    """Sanitize JSON string by escaping literal newlines inside string values.
    
    LLMs sometimes return JSON with unescaped newlines in string values,
    which causes JSON decode errors. This function escapes them.
    
    Args:
        json_str: Raw JSON string that may have unescaped newlines.
        
    Returns:
        Sanitized JSON string with escaped newlines.
    """
    # Replace literal newlines that are inside strings with escaped versions
    # This regex finds content between quotes and escapes newlines within
    result = []
    in_string = False
    escape_next = False
    
    for char in json_str:
        if escape_next:
            result.append(char)
            escape_next = False
            continue
            
        if char == '\\':
            result.append(char)
            escape_next = True
            continue
            
        if char == '"' and not escape_next:
            in_string = not in_string
            result.append(char)
            continue
            
        if in_string and char == '\n':
            result.append('\\n')
            continue
            
        if in_string and char == '\r':
            result.append('\\r')
            continue
            
        if in_string and char == '\t':
            result.append('\\t')
            continue
            
        result.append(char)
    
    return ''.join(result)


def _parse_decision_response(response_text: str) -> dict[str, Any]:
    """Parse LLM response to extract decision JSON.

    Args:
        response_text: Raw LLM response.

    Returns:
        Parsed decision dictionary.
    """
    # Look for JSON block in markdown
    json_match = re.search(r"```json\s*(.*?)\s*```", response_text, re.DOTALL)
    if json_match:
        try:
            result = json.loads(_sanitize_json_string(json_match.group(1)))
            logger.debug("parsed_json_from_markdown_block")
            return result
        except json.JSONDecodeError as e:
            logger.warning("json_decode_failed_markdown", error=str(e), content=json_match.group(1)[:500])

    # Try to find raw JSON object (improved regex for nested objects)
    json_match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", response_text, re.DOTALL)
    if json_match:
        try:
            result = json.loads(_sanitize_json_string(json_match.group(0)))
            logger.debug("parsed_json_from_raw")
            return result
        except json.JSONDecodeError as e:
            logger.warning("json_decode_failed_raw", error=str(e), content=json_match.group(0)[:500])

    # Fallback: try to parse entire response as JSON
    try:
        result = json.loads(_sanitize_json_string(response_text))
        logger.debug("parsed_json_from_full_response")
        return result
    except json.JSONDecodeError as e:
        logger.warning("json_decode_failed_full", error=str(e))

    # Last resort: extract fields manually
    # Log the full response so we can debug why parsing failed
    logger.error(
        "llm_response_parse_failed",
        response_text=response_text[:1000],
        response_length=len(response_text),
    )

    result = {
        "next_action": "ENRICH",
        "action_reasoning": "Failed to parse LLM response",
        "tp_confidence": 0.5,
        "confidence_reasoning": "Unable to determine",
        "specific_instructions": "",  # Add default to prevent None
    }

    # Try to extract action
    for action in ["VERDICT", "CLOSE", "INVESTIGATE", "CONTEXTUALIZE", "ENRICH"]:
        if action in response_text.upper():
            result["next_action"] = action
            break

    return result
