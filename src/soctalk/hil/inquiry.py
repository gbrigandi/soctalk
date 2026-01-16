"""Inquiry handler for conversational HIL interactions."""

from __future__ import annotations

from typing import Any, Optional

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from soctalk.config import get_config
from soctalk.llm import create_chat_model

logger = structlog.get_logger()

INQUIRY_SYSTEM_PROMPT = """You are a Security Operations Center (SOC) analyst assistant helping a human analyst understand a security investigation.

You have access to the full context of an ongoing investigation, including:
- Alert details from Wazuh SIEM
- Enrichment results from Cortex analyzers
- Threat intelligence context from MISP
- Findings generated during the investigation
- The AI verdict and recommendation

Your role is to:
1. Answer the analyst's questions clearly and concisely
2. Explain technical details in an accessible way
3. Provide relevant context from the investigation data
4. Help the analyst make an informed decision

Be direct and factual. If something is uncertain or requires more investigation, say so.
Do not make up information that isn't in the investigation context.
"""

INQUIRY_USER_TEMPLATE = """## Investigation Context

**Investigation ID:** {investigation_id}
**Title:** {title}
**Severity:** {severity}
**Alert Count:** {alert_count}

### Alerts
{alerts_section}

### Observables
{observables_section}

### Enrichment Results
{enrichments_section}

### MISP Threat Intelligence
{misp_section}

### Key Findings
{findings_section}

### AI Verdict
{verdict_section}

---

## Conversation History
{conversation_history}

---

## Analyst Question
{inquiry}

---

Please answer the analyst's question based on the investigation context above.
"""


async def handle_inquiry(
    investigation_id: str,
    inquiry: str,
    state: dict[str, Any],
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> str:
    """Handle a user inquiry about an investigation.

    Args:
        investigation_id: The investigation ID.
        inquiry: The user's question.
        state: The current LangGraph state containing investigation data.
        conversation_history: Optional list of previous Q&A exchanges.

    Returns:
        The LLM's response to the inquiry.
    """
    logger.info(
        "handling_inquiry",
        investigation_id=investigation_id,
        inquiry_length=len(inquiry),
    )

    config = get_config()
    investigation = state.get("investigation", {})
    verdict = state.get("verdict", {})

    # Build context for the LLM
    context = _build_inquiry_context(
        investigation_id=investigation_id,
        investigation=investigation,
        verdict=verdict,
        inquiry=inquiry,
        conversation_history=conversation_history or [],
    )

    # Call the LLM
    llm = create_chat_model(
        config.llm,
        model=config.llm.fast_model,
        temperature=0.3,  # Slightly higher for more natural conversation
        max_tokens=1024,
    )

    messages = [
        SystemMessage(content=INQUIRY_SYSTEM_PROMPT),
        HumanMessage(content=INQUIRY_USER_TEMPLATE.format(**context)),
    ]

    try:
        response = await llm.ainvoke(messages)
        response_text = response.content

        logger.info(
            "inquiry_response_generated",
            investigation_id=investigation_id,
            response_length=len(response_text),
        )

        return response_text

    except Exception as e:
        logger.error(
            "inquiry_handler_error",
            investigation_id=investigation_id,
            error=str(e),
        )
        return f"I apologize, but I encountered an error while processing your question: {str(e)}"


def _build_inquiry_context(
    investigation_id: str,
    investigation: dict[str, Any],
    verdict: dict[str, Any],
    inquiry: str,
    conversation_history: list[dict[str, str]],
) -> dict[str, str]:
    """Build the context dictionary for the inquiry prompt.

    Args:
        investigation_id: The investigation ID.
        investigation: Investigation data from state.
        verdict: Verdict data from state.
        inquiry: The user's question.
        conversation_history: Previous Q&A exchanges.

    Returns:
        Dictionary of formatted context sections.
    """
    # Format alerts
    alerts = investigation.get("alerts", [])
    alerts_lines = []
    for i, alert in enumerate(alerts[:10], 1):  # Limit to 10 alerts
        severity = alert.get("severity", "unknown")
        desc = alert.get("rule_description", "No description")
        agent = alert.get("source", {}).get("agent_name", "unknown")
        timestamp = alert.get("timestamp", "unknown")
        rule_id = alert.get("rule_id", "unknown")

        alerts_lines.append(
            f"{i}. **[{severity.upper()}]** {desc}\n"
            f"   - Rule ID: {rule_id}\n"
            f"   - Agent: {agent}\n"
            f"   - Time: {timestamp}"
        )
    alerts_section = "\n\n".join(alerts_lines) if alerts_lines else "No alerts"

    # Format observables
    observables = investigation.get("observables", [])
    obs_lines = []
    for obs in observables[:20]:  # Limit to 20 observables
        obs_type = obs.get("type", "unknown")
        obs_value = obs.get("value", "unknown")
        obs_lines.append(f"- **{obs_type}:** {obs_value}")
    observables_section = "\n".join(obs_lines) if obs_lines else "No observables"

    # Format enrichments
    enrichments = investigation.get("enrichments", [])
    enrich_lines = []
    for e in enrichments[:15]:  # Limit to 15 enrichments
        analyzer = e.get("analyzer", "unknown")
        obs = e.get("observable", {})
        obs_value = obs.get("value", "unknown")
        verdict_val = e.get("verdict", "unknown")
        confidence = e.get("confidence", 0)
        details = e.get("details", {})

        # Format verdict with emoji
        verdict_emoji = {"malicious": "🔴", "suspicious": "⚠️", "benign": "✅"}.get(
            verdict_val, "❓"
        )

        enrich_lines.append(
            f"- {verdict_emoji} **{obs_value}** via {analyzer}: {verdict_val} ({confidence:.0%})"
        )

        # Add key details if available
        if details:
            for key, value in list(details.items())[:3]:
                if value and str(value).strip():
                    enrich_lines.append(f"  - {key}: {value}")

    enrichments_section = "\n".join(enrich_lines) if enrich_lines else "No enrichments"

    # Format MISP context
    misp_context = investigation.get("misp_context") or {}
    if misp_context:
        misp_lines = [
            f"- IOCs checked: {len(misp_context.get('checked_iocs', []))}",
            f"- IOCs matched: {len(misp_context.get('matches', []))}",
        ]

        threat_actors = misp_context.get("threat_actors", [])
        if threat_actors:
            misp_lines.append(f"- Threat actors: {', '.join(threat_actors[:5])}")

        campaigns = misp_context.get("campaigns", [])
        if campaigns:
            misp_lines.append(f"- Campaigns: {', '.join(campaigns[:5])}")

        warninglist_hits = misp_context.get("warninglist_hits", [])
        if warninglist_hits:
            misp_lines.append(f"- Warninglist hits: {len(warninglist_hits)}")

        # Add matched events
        events = misp_context.get("events", {})
        if events:
            misp_lines.append("\n**Matched Events:**")
            for event_id, event_data in list(events.items())[:3]:
                info = event_data.get("info", "No info")
                threat_level = event_data.get("threat_level", "unknown")
                misp_lines.append(f"  - Event {event_id}: {info} (Threat: {threat_level})")

        misp_section = "\n".join(misp_lines)
    else:
        misp_section = "No MISP context available"

    # Format findings
    findings = investigation.get("findings", [])
    findings_lines = []
    for f in findings[:10]:  # Limit to 10 findings
        if isinstance(f, dict):
            severity = f.get("severity", "unknown")
            desc = f.get("description", "No description")
            evidence = f.get("evidence", [])

            findings_lines.append(f"- **[{severity.upper()}]** {desc}")
            for ev in evidence[:3]:
                findings_lines.append(f"  - {ev}")
        else:
            findings_lines.append(f"- {str(f)}")

    findings_section = "\n".join(findings_lines) if findings_lines else "No findings"

    # Format verdict
    if verdict:
        decision = verdict.get("decision", "unknown")
        confidence = verdict.get("confidence", 0)
        assessment = verdict.get("threat_assessment", "No assessment")
        recommendation = verdict.get("recommendation", "No recommendation")
        key_evidence = verdict.get("key_evidence", [])
        gaps = verdict.get("gaps_in_evidence", [])

        verdict_lines = [
            f"**Decision:** {decision.upper()}",
            f"**Confidence:** {confidence:.0%}",
            f"**Assessment:** {assessment}",
            f"**Recommendation:** {recommendation}",
        ]

        if key_evidence:
            verdict_lines.append("\n**Key Evidence:**")
            for ev in key_evidence[:5]:
                verdict_lines.append(f"  - {ev}")

        if gaps:
            verdict_lines.append("\n**Gaps in Evidence:**")
            for gap in gaps[:3]:
                verdict_lines.append(f"  - {gap}")

        verdict_section = "\n".join(verdict_lines)
    else:
        verdict_section = "No verdict available"

    # Format conversation history
    if conversation_history:
        history_lines = []
        for exchange in conversation_history[-5:]:  # Last 5 exchanges
            q = exchange.get("question", "")
            a = exchange.get("answer", "")
            history_lines.append(f"**Analyst:** {q}\n**Assistant:** {a}")
        history_section = "\n\n".join(history_lines)
    else:
        history_section = "No previous conversation"

    return {
        "investigation_id": investigation_id,
        "title": investigation.get("title", "Unknown Investigation"),
        "severity": investigation.get("max_severity", "unknown"),
        "alert_count": len(alerts),
        "alerts_section": alerts_section,
        "observables_section": observables_section,
        "enrichments_section": enrichments_section,
        "misp_section": misp_section,
        "findings_section": findings_section,
        "verdict_section": verdict_section,
        "conversation_history": history_section,
        "inquiry": inquiry,
    }
