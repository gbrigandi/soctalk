"""LangGraph builder for SecOps agent."""

from __future__ import annotations

from typing import Any, Literal

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from soctalk.models.enums import HumanDecision, Phase, VerdictDecision
from soctalk.supervisor.node import supervisor_node
from soctalk.supervisor.verdict import verdict_node
from soctalk.workers.wazuh import wazuh_worker_node
from soctalk.workers.cortex import cortex_worker_node
from soctalk.workers.misp import misp_worker_node
from soctalk.workers.thehive import thehive_worker_node
from soctalk.graph.hil import human_review_node
from soctalk.graph.close import close_investigation_node

logger = structlog.get_logger()


def route_from_supervisor(state: dict[str, Any]) -> Literal[
    "wazuh_worker",
    "cortex_worker",
    "misp_worker",
    "verdict",
    "close_investigation",
]:
    """Route from supervisor to next node based on decision.

    Args:
        state: Current graph state.

    Returns:
        Next node name.
    """
    decision = state.get("supervisor_decision", {})
    action = decision.get("next_action", "ENRICH")

    logger.debug("routing_from_supervisor", action=action)

    if action == "INVESTIGATE":
        return "wazuh_worker"
    elif action == "ENRICH":
        return "cortex_worker"
    elif action == "CONTEXTUALIZE":
        return "misp_worker"
    elif action == "VERDICT":
        return "verdict"
    elif action == "CLOSE":
        return "close_investigation"
    else:
        # Default to enrichment
        return "cortex_worker"


def route_from_verdict(state: dict[str, Any]) -> Literal[
    "human_review",
    "close_investigation",
    "supervisor",
]:
    """Route from verdict to next node based on verdict decision.

    Args:
        state: Current graph state.

    Returns:
        Next node name.
    """
    verdict = state.get("verdict", {})
    decision = verdict.get("decision", VerdictDecision.NEEDS_MORE_INFO.value)

    logger.debug("routing_from_verdict", decision=decision)

    if decision == VerdictDecision.ESCALATE.value:
        return "human_review"
    elif decision == VerdictDecision.CLOSE.value:
        return "close_investigation"
    elif decision == VerdictDecision.NEEDS_MORE_INFO.value:
        # Read verdict retry count (already incremented by verdict_node)
        verdict_retries = state.get("verdict_retry_count", 0)

        # After 2 retries, force escalation to human review
        if verdict_retries >= 2:
            logger.warning(
                "verdict_max_retries_reached",
                retries=verdict_retries,
                decision="forcing_human_review",
            )
            return "human_review"

        return "supervisor"
    else:
        return "human_review"


def route_from_human_review(state: dict[str, Any]) -> Literal[
    "thehive_worker",
    "close_investigation",
    "supervisor",
]:
    """Route from human review based on decision.

    Args:
        state: Current graph state.

    Returns:
        Next node name.
    """
    decision = state.get("human_decision")

    logger.debug("routing_from_human_review", decision=decision)

    if decision == HumanDecision.APPROVE.value:
        return "thehive_worker"
    elif decision == HumanDecision.REJECT.value:
        return "close_investigation"
    elif decision == HumanDecision.MORE_INFO.value:
        return "supervisor"
    else:
        # Default to close
        return "close_investigation"


def build_secops_graph(
    checkpointer: BaseCheckpointSaver | None = None,
) -> StateGraph:
    """Build the SecOps LangGraph.

    Args:
        checkpointer: Optional checkpoint saver for state persistence.
            When provided, enables workflow resumption and HIL pausing.

    Returns:
        Compiled StateGraph ready for execution.

    Graph structure:
        START -> supervisor
        supervisor -> [wazuh_worker | cortex_worker | misp_worker | verdict | close_investigation]
        wazuh_worker -> supervisor
        cortex_worker -> supervisor
        misp_worker -> supervisor
        verdict -> [human_review | close_investigation | supervisor]
        human_review -> [thehive_worker | close_investigation | supervisor]
        thehive_worker -> close_investigation
        close_investigation -> END

    Example with checkpointing:
        async with get_checkpointer() as checkpointer:
            graph = build_secops_graph(checkpointer=checkpointer)
            config = get_checkpoint_config(investigation_id)
            result = await graph.ainvoke(state, config=config)
    """
    logger.info("building_secops_graph", checkpointer_enabled=checkpointer is not None)

    # Create graph with dict state
    graph = StateGraph(dict)

    # Add nodes
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("wazuh_worker", wazuh_worker_node)
    graph.add_node("cortex_worker", cortex_worker_node)
    graph.add_node("misp_worker", misp_worker_node)
    graph.add_node("verdict", verdict_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("thehive_worker", thehive_worker_node)
    graph.add_node("close_investigation", close_investigation_node)

    # Set entry point
    graph.set_entry_point("supervisor")

    # Add conditional edges from supervisor
    graph.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "wazuh_worker": "wazuh_worker",
            "cortex_worker": "cortex_worker",
            "misp_worker": "misp_worker",
            "verdict": "verdict",
            "close_investigation": "close_investigation",
        },
    )

    # Workers return to supervisor
    graph.add_edge("wazuh_worker", "supervisor")
    graph.add_edge("cortex_worker", "supervisor")
    graph.add_edge("misp_worker", "supervisor")

    # Verdict routes to HIL, close, or back to supervisor
    graph.add_conditional_edges(
        "verdict",
        route_from_verdict,
        {
            "human_review": "human_review",
            "close_investigation": "close_investigation",
            "supervisor": "supervisor",
        },
    )

    # Human review routes to TheHive, close, or back to supervisor
    graph.add_conditional_edges(
        "human_review",
        route_from_human_review,
        {
            "thehive_worker": "thehive_worker",
            "close_investigation": "close_investigation",
            "supervisor": "supervisor",
        },
    )

    # TheHive leads to close
    graph.add_edge("thehive_worker", "close_investigation")

    # Close leads to end
    graph.add_edge("close_investigation", END)

    # Compile the graph with optional checkpointer
    compiled = graph.compile(checkpointer=checkpointer)

    logger.info("secops_graph_built", checkpointer_enabled=checkpointer is not None)

    return compiled


def get_graph_visualization() -> str:
    """Get a text visualization of the graph.

    Returns:
        ASCII art representation of the graph.
    """
    return """
    SecOps Agent Graph
    ==================

                        ┌─────────────────┐
                        │     START       │
                        └────────┬────────┘
                                 │
                                 ▼
                        ┌─────────────────┐
              ┌────────▶│   SUPERVISOR    │◀────────┐
              │         └────────┬────────┘         │
              │                  │                  │
              │   ┌──────────────┼──────────────┐   │
              │   │              │              │   │
              │   ▼              ▼              ▼   │
        ┌───────────┐    ┌───────────┐    ┌─────────────┐
        │  WAZUH    │    │  CORTEX   │    │   VERDICT   │
        │  WORKER   │    │  WORKER   │    │ (Reasoning) │
        └─────┬─────┘    └─────┬─────┘    └──────┬──────┘
              │                │                 │
              └────────────────┘    ┌────────────┼────────────┐
                      │             │            │            │
                      │             ▼            ▼            ▼
                      │      ┌───────────┐  ┌─────────┐  (back to
                      │      │  HUMAN    │  │  CLOSE  │  supervisor)
                      │      │  REVIEW   │  │         │
                      │      └─────┬─────┘  └────┬────┘
                      │            │              │
                      │   ┌────────┼────────┐    │
                      │   │        │        │    │
                      │   ▼        ▼        ▼    │
                      │ ┌──────┐ ┌─────┐ (back)  │
                      │ │THEHIVE│ │CLOSE│        │
                      │ │WORKER│ │     │        │
                      │ └──┬───┘ └──┬──┘        │
                      │    │        │           │
                      │    └────┬───┘           │
                      │         │               │
                      │         ▼               │
                      │    ┌─────────┐          │
                      └───▶│  CLOSE  │◀─────────┘
                           │         │
                           └────┬────┘
                                │
                                ▼
                           ┌─────────┐
                           │   END   │
                           └─────────┘
    """
