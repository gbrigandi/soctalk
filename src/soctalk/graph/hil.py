"""Human-in-the-Loop review node."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from soctalk.models.enums import HumanDecision
from soctalk.models.investigation import Investigation
from soctalk.models.verdict import Verdict
from soctalk.persistence.emitter import get_emitter_from_config, get_investigation_id_from_state

if TYPE_CHECKING:
    from soctalk.hil.service import HILService

logger = structlog.get_logger()

_HUMAN_REVIEW_REQUESTED_FLAG = "_human_review_requested_emitted"


async def human_review_node(
    state: dict[str, Any],
    run_config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """Human-in-the-Loop review node.

    Presents the investigation summary and verdict to a human analyst
    for approval before creating an incident.

    Supports multiple backends:
    - Slack: Uses configured HIL service
    - Dashboard: Uses LangGraph interrupt + UI decision
    - CLI: Local interactive prompt

    Args:
        state: Current graph state.

    Returns:
        Updated state with human decision.
    """
    logger.info("human_review_started")

    state["awaiting_human_approval"] = True

    # Get investigation and verdict
    investigation_data = state.get("investigation", {})
    verdict_data = state.get("verdict", {})

    emitter = get_emitter_from_config(run_config)
    investigation_id = get_investigation_id_from_state(state)
    if emitter and investigation_id and not state.get(_HUMAN_REVIEW_REQUESTED_FLAG):
        try:
            verdict_decision = verdict_data.get("decision", "unknown") if verdict_data else "unknown"
            # Handle enum or string value
            if hasattr(verdict_decision, "value"):
                verdict_decision = verdict_decision.value
            verdict_confidence = verdict_data.get("confidence", 0.0) if verdict_data else 0.0
            await emitter.emit_human_review_requested(
                investigation_id=investigation_id,
                reason="Verdict requires human approval before escalation",
                verdict_decision=str(verdict_decision),
                verdict_confidence=float(verdict_confidence),
            )
            state[_HUMAN_REVIEW_REQUESTED_FLAG] = True
        except Exception as emit_error:
            logger.warning("event_emission_failed", error=str(emit_error))

    investigation = Investigation(**investigation_data) if isinstance(investigation_data, dict) else investigation_data
    verdict = Verdict(**verdict_data) if verdict_data and isinstance(verdict_data, dict) else None

    hil_service: Optional[HILService] = None
    hil_backend = "cli"
    if run_config:
        configurable = run_config.get("configurable") or {}
        hil_service = configurable.get("hil_service")
        hil_backend = configurable.get("hil_backend") or hil_backend

    try:
        if hil_service and hil_service.is_connected:
            # Use configured HIL backend (Slack, Discord, etc.)
            decision, feedback = await _present_via_hil_service(
                hil_service, investigation, verdict, state
            )
            emit_decision_event = True
            reviewer = None
        else:
            decision, feedback, reviewer, emit_decision_event = await _handle_noninteractive_review(
                hil_backend=hil_backend,
                investigation=investigation,
                verdict=verdict,
            )

        state["human_decision"] = decision.value
        state["human_feedback"] = feedback
        state["awaiting_human_approval"] = False

        logger.info(
            "human_decision_received",
            decision=decision.value,
            has_feedback=bool(feedback),
        )

        # Emit human decision received event
        if emit_decision_event and emitter and investigation_id:
            try:
                await emitter.emit_human_decision_received(
                    investigation_id=investigation_id,
                    decision=decision.value,
                    feedback=feedback,
                    reviewer=reviewer,
                )
            except Exception as emit_error:
                logger.warning("event_emission_failed", error=str(emit_error))

    except GraphInterrupt:
        raise
    except Exception as e:
        logger.error("human_review_error", error=str(e))
        # Default to requiring more info on error
        state["human_decision"] = HumanDecision.MORE_INFO.value
        state["human_feedback"] = f"Error during review: {str(e)}"
        state["awaiting_human_approval"] = False

    state["last_updated"] = datetime.now().isoformat()
    return state


async def _handle_noninteractive_review(
    *,
    hil_backend: str,
    investigation: Investigation,
    verdict: Verdict | None,
) -> tuple[HumanDecision, str | None, str | None, bool]:
    if hil_backend == "cli":
        if sys.stdin and sys.stdin.isatty():
            decision, feedback = await _prompt_cli_decision(investigation, verdict)
            return decision, feedback, None, True
        hil_backend = "dashboard"

    if hil_backend not in ("dashboard", "ui"):
        logger.warning("unknown_hil_backend", hil_backend=hil_backend)

    try:
        resume_value = interrupt(
            {
                "type": "human_review",
                "investigation_id": investigation.id,
                "title": investigation.title,
                "verdict": verdict.model_dump() if verdict else None,
            }
        )
    except GraphInterrupt:
        raise
    except Exception as e:
        logger.error("hil_interrupt_failed", error=str(e))
        return HumanDecision.MORE_INFO, "HIL interrupt failed - manual follow-up required", None, True

    payload = resume_value if isinstance(resume_value, dict) else {"decision": resume_value}
    decision_str = payload.get("decision")
    decision = _coerce_human_decision(decision_str)
    feedback = payload.get("feedback")
    reviewer = payload.get("reviewer")
    source = payload.get("source")
    emit_event = source not in ("dashboard", "ui")
    return decision, feedback, reviewer, emit_event


def _coerce_human_decision(value: Any) -> HumanDecision:
    if isinstance(value, HumanDecision):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == HumanDecision.APPROVE.value:
            return HumanDecision.APPROVE
        if normalized == HumanDecision.REJECT.value:
            return HumanDecision.REJECT
        if normalized == HumanDecision.MORE_INFO.value:
            return HumanDecision.MORE_INFO
    return HumanDecision.MORE_INFO


async def _prompt_cli_decision(
    investigation: Investigation,
    verdict: Verdict | None,
) -> tuple[HumanDecision, str | None]:
    prompt_lines = [
        "",
        f"Human review required for: {investigation.title} ({investigation.id})",
    ]
    if verdict:
        prompt_lines.append(
            f"Verdict: {verdict.decision.value} (confidence {verdict.confidence:.0%})"
        )
    prompt_lines.append("Decision [a]pprove / [r]eject / [m]ore info (default: m): ")

    def _read() -> str:
        return input("\n".join(prompt_lines))

    choice = (await asyncio.get_running_loop().run_in_executor(None, _read)).strip().lower()
    if choice in ("a", "approve"):
        return HumanDecision.APPROVE, None
    if choice in ("r", "reject"):
        return HumanDecision.REJECT, None
    return HumanDecision.MORE_INFO, None


async def _present_via_hil_service(
    hil_service: "HILService",
    investigation: Investigation,
    verdict: Optional[Verdict],
    state: dict[str, Any],
) -> tuple[HumanDecision, Optional[str]]:
    """Present investigation via HIL service (Slack, Discord, etc.).

    Args:
        hil_service: The HIL service to use.
        investigation: Investigation to review.
        verdict: Verdict from reasoning LLM.
        state: Current LangGraph state for conversational HIL.

    Returns:
        Tuple of (decision, optional feedback).
    """
    from soctalk.hil.base import HILTimeoutError, HILConnectionError

    logger.info(
        "hil_presenting_via_service",
        backend=hil_service.backend_name,
        investigation_id=investigation.id,
    )

    try:
        response = await hil_service.request_approval(
            investigation=investigation,
            verdict=verdict,
            state=state,
        )

        logger.info(
            "hil_response_received",
            decision=response.decision.value,
            reviewer=response.reviewer,
            response_time=response.response_time_seconds,
        )

        return response.decision, response.feedback

    except HILTimeoutError:
        logger.warning("hil_timeout", investigation_id=investigation.id)
        # On timeout, request more info rather than auto-approving
        return HumanDecision.MORE_INFO, "HIL review timed out - please review manually"

    except HILConnectionError as e:
        logger.error("hil_connection_error", error=str(e))
        # On connection error, fall back to more info
        return HumanDecision.MORE_INFO, f"HIL connection error: {e}"
