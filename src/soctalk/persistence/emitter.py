"""Event emitter for LangGraph nodes to emit business events."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.persistence.events import EventType
from soctalk.persistence.projector import ProjectingEventStore
from soctalk.persistence.store import EventStore

logger = structlog.get_logger()


class EventEmitter:
    """Helper class for emitting events from LangGraph nodes.

    This wraps the ProjectingEventStore with convenience methods
    for emitting specific event types with proper data structure.

    Usage in LangGraph nodes:
        emitter = state.get("event_emitter")
        if emitter:
            await emitter.emit_investigation_created(investigation_id, data)
    """

    def __init__(self, session: AsyncSession):
        """Initialize the emitter.

        Args:
            session: Async database session to use for event storage.
        """
        self.session = session
        self.store = EventStore(session)
        self.projecting_store = ProjectingEventStore(session)
        self._version_cache: dict[UUID, int] = {}

    async def _get_current_version(self, aggregate_id: UUID) -> int:
        """Get the current version for an aggregate (for optimistic concurrency).

        Caches versions to avoid repeated DB queries within a session.
        After successful append, caller should call _increment_version().

        Args:
            aggregate_id: The aggregate UUID.

        Returns:
            Current version number (0 for new aggregates).
        """
        if aggregate_id not in self._version_cache:
            events = await self.store.get_events(aggregate_id)
            self._version_cache[aggregate_id] = len(events)
        return self._version_cache[aggregate_id]

    def _increment_version(self, aggregate_id: UUID) -> None:
        """Increment cached version after successful append."""
        if aggregate_id in self._version_cache:
            self._version_cache[aggregate_id] += 1

    async def emit_investigation_created(
        self,
        investigation_id: UUID,
        title: str,
        alert_ids: list[str],
        max_severity: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Emit an investigation created event.

        Args:
            investigation_id: UUID of the investigation.
            title: Investigation title.
            alert_ids: List of correlated alert IDs.
            max_severity: Maximum severity level.
            idempotency_key: Optional idempotency key.
        """
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.INVESTIGATION_CREATED,
            data={
                "title": title,
                "alert_ids": alert_ids,
                "max_severity": max_severity,
            },
            idempotency_key=idempotency_key,
        )
        logger.debug("emitted_investigation_created", investigation_id=str(investigation_id))

    async def emit_investigation_started(
        self,
        investigation_id: UUID,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Emit an investigation started event."""
        data: dict[str, Any] = {}
        if title:
            data["title"] = title

        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.INVESTIGATION_STARTED,
            data=data,
            idempotency_key=idempotency_key,
        )
        logger.debug("emitted_investigation_started", investigation_id=str(investigation_id))

    async def emit_alert_correlated(
        self,
        investigation_id: UUID,
        alert_id: str,
        rule_id: str,
        rule_description: str,
        severity: str,
        observable_count: int,
    ) -> None:
        """Emit an alert correlated event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.ALERT_CORRELATED,
            data={
                "alert_id": alert_id,
                "rule_id": rule_id,
                "rule_description": rule_description,
                "severity": severity,
                "observable_count": observable_count,
            },
        )

    async def emit_observable_extracted(
        self,
        investigation_id: UUID,
        observable_type: str,
        observable_value: str,
        source: str,
    ) -> None:
        """Emit an observable extracted event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.OBSERVABLE_EXTRACTED,
            data={
                "type": observable_type,
                "value": observable_value,
                "source": source,
            },
        )

    async def emit_phase_changed(
        self,
        investigation_id: UUID,
        from_phase: str,
        to_phase: str,
    ) -> None:
        """Emit a phase changed event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.PHASE_CHANGED,
            data={
                "from_phase": from_phase,
                "to_phase": to_phase,
            },
        )

    async def emit_enrichment_requested(
        self,
        investigation_id: UUID,
        observable_type: str,
        observable_value: str,
        analyzer: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Emit an enrichment requested event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.ENRICHMENT_REQUESTED,
            data={
                "observable_type": observable_type,
                "observable_value": observable_value,
                "analyzer": analyzer,
            },
            idempotency_key=idempotency_key,
        )

    async def emit_enrichment_completed(
        self,
        investigation_id: UUID,
        observable_type: str,
        observable_value: str,
        analyzer: str,
        verdict: str,
        score: float | None,
        response_time_ms: int,
    ) -> None:
        """Emit an enrichment completed event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.ENRICHMENT_COMPLETED,
            data={
                "observable_type": observable_type,
                "observable_value": observable_value,
                "analyzer": analyzer,
                "verdict": verdict,
                "score": score,
                "response_time_ms": response_time_ms,
            },
        )

    async def emit_enrichment_failed(
        self,
        investigation_id: UUID,
        observable_type: str,
        observable_value: str,
        analyzer: str,
        error: str,
    ) -> None:
        """Emit an enrichment failed event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.ENRICHMENT_FAILED,
            data={
                "observable_type": observable_type,
                "observable_value": observable_value,
                "analyzer": analyzer,
                "error": error,
            },
        )

    async def emit_supervisor_decision(
        self,
        investigation_id: UUID,
        action: str,
        reasoning: str,
        tp_confidence: float,
        iteration: int,
    ) -> None:
        """Emit a supervisor decision event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.SUPERVISOR_DECISION,
            data={
                "action": action,
                "reasoning": reasoning,
                "tp_confidence": tp_confidence,
                "iteration": iteration,
            },
        )

    async def emit_verdict_rendered(
        self,
        investigation_id: UUID,
        decision: str,
        confidence: float,
        reasoning: str,
        threat_actor: str | None,
    ) -> None:
        """Emit a verdict rendered event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.VERDICT_RENDERED,
            data={
                "decision": decision,
                "confidence": confidence,
                "reasoning": reasoning,
                "threat_actor": threat_actor,
            },
        )

    async def emit_human_review_requested(
        self,
        investigation_id: UUID,
        reason: str,
        verdict_decision: str,
        verdict_confidence: float,
    ) -> None:
        """Emit a human review requested event.

        Note: This commits immediately so the pending review is visible
        in the dashboard while waiting for the human decision.
        """
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.HUMAN_REVIEW_REQUESTED,
            data={
                "reason": reason,
                "verdict_decision": verdict_decision,
                "verdict_confidence": verdict_confidence,
            },
        )
        # Commit immediately so pending review appears in dashboard
        await self.session.commit()

    async def emit_human_decision_received(
        self,
        investigation_id: UUID,
        decision: str,
        feedback: str | None,
        reviewer: str | None,
    ) -> None:
        """Emit a human decision received event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.HUMAN_DECISION_RECEIVED,
            data={
                "decision": decision,
                "feedback": feedback,
                "reviewer": reviewer,
            },
        )

    async def emit_thehive_case_created(
        self,
        investigation_id: UUID,
        case_id: str,
        case_number: str | None,
        title: str,
        idempotency_key: str | None = None,
    ) -> None:
        """Emit a TheHive case created event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.THEHIVE_CASE_CREATED,
            data={
                "case_id": case_id,
                "case_number": case_number,
                "title": title,
            },
            idempotency_key=idempotency_key,
        )

    async def emit_investigation_closed(
        self,
        investigation_id: UUID,
        status: str,
        resolution: str,
        verdict_decision: str | None,
        thehive_case_id: str | None,
        duration_seconds: int,
    ) -> None:
        """Emit an investigation closed event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.INVESTIGATION_CLOSED,
            data={
                "status": status,
                "resolution": resolution,
                "verdict_decision": verdict_decision,
                "thehive_case_id": thehive_case_id,
                "duration_seconds": duration_seconds,
            },
        )

    async def emit_misp_context_retrieved(
        self,
        investigation_id: UUID,
        observable_type: str,
        observable_value: str,
        event_count: int,
        threat_actors: list[str],
    ) -> None:
        """Emit a MISP context retrieved event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.MISP_CONTEXT_RETRIEVED,
            data={
                "observable_type": observable_type,
                "observable_value": observable_value,
                "event_count": event_count,
                "threat_actors": threat_actors,
            },
        )

    async def emit_wazuh_forensics_collected(
        self,
        investigation_id: UUID,
        agent_id: str,
        data_types: list[str],
        process_count: int,
        port_count: int,
    ) -> None:
        """Emit a Wazuh forensics collected event."""
        await self.projecting_store.append(
            aggregate_id=investigation_id,
            aggregate_type="Investigation",
            event_type=EventType.WAZUH_FORENSICS_COLLECTED,
            data={
                "agent_id": agent_id,
                "data_types": data_types,
                "process_count": process_count,
                "port_count": port_count,
            },
        )


def get_emitter_from_state(state: dict[str, Any]) -> EventEmitter | None:
    """Get the event emitter from graph state.

    Args:
        state: LangGraph state dictionary.

    Returns:
        EventEmitter if available, None otherwise.
    """
    return state.get("event_emitter")


def get_emitter_from_config(config: RunnableConfig | None) -> EventEmitter | None:
    """Get the event emitter from run configuration.

    This is preferred over storing runtime objects in graph state because
    state may be persisted by LangGraph checkpointers.
    """
    if not config:
        return None
    configurable = config.get("configurable") or {}
    return configurable.get("event_emitter")


def get_investigation_id_from_state(state: dict[str, Any]) -> UUID | None:
    """Get the investigation ID from graph state.

    Args:
        state: LangGraph state dictionary.

    Returns:
        Investigation UUID if available, None otherwise.
    """
    investigation = state.get("investigation", {})
    if isinstance(investigation, dict):
        inv_id = investigation.get("id")
        if inv_id:
            return UUID(inv_id) if isinstance(inv_id, str) else inv_id
    return None
