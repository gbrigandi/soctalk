"""Projector for syncing events to read models (CQRS projections)."""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.persistence.events import EventType
from soctalk.persistence.models import (
    AnalyzerStats,
    Event,
    InvestigationReadModel,
    IOCStats,
    MetricsHourly,
    PendingReview,
    RuleStats,
)

logger = structlog.get_logger()


class Projector:
    """Projects events to read models for CQRS pattern.

    This projector runs synchronously within the same transaction as the event
    append, ensuring consistency between events and read models.
    """

    def __init__(self, session: AsyncSession):
        """Initialize the projector with a database session.

        Args:
            session: Async SQLAlchemy session for database operations
        """
        self.session = session

    async def project(self, event: Event) -> None:
        """Project an event to the appropriate read models.

        Args:
            event: The event to project
        """
        event_type = event.event_type

        # Route to appropriate projection handler
        if event_type == EventType.INVESTIGATION_CREATED.value:
            await self._project_investigation_created(event)
        elif event_type == EventType.INVESTIGATION_STARTED.value:
            await self._project_investigation_started(event)
        elif event_type == EventType.INVESTIGATION_PAUSED.value:
            await self._project_investigation_paused(event)
        elif event_type == EventType.INVESTIGATION_RESUMED.value:
            await self._project_investigation_resumed(event)
        elif event_type == EventType.INVESTIGATION_CANCELLED.value:
            await self._project_investigation_cancelled(event)
        elif event_type == EventType.ALERT_CORRELATED.value:
            await self._project_alert_correlated(event)
        elif event_type == EventType.OBSERVABLE_EXTRACTED.value:
            await self._project_observable_extracted(event)
        elif event_type == EventType.ENRICHMENT_COMPLETED.value:
            await self._project_enrichment_completed(event)
        elif event_type == EventType.VERDICT_RENDERED.value:
            await self._project_verdict_rendered(event)
        elif event_type == EventType.INVESTIGATION_ESCALATED.value:
            await self._project_investigation_escalated(event)
        elif event_type == EventType.INVESTIGATION_AUTO_CLOSED.value:
            await self._project_investigation_auto_closed(event)
        elif event_type == EventType.INVESTIGATION_CLOSED.value:
            await self._project_investigation_closed(event)
        elif event_type == EventType.THEHIVE_CASE_CREATED.value:
            await self._project_thehive_case_created(event)
        elif event_type == EventType.ANALYZER_INVOKED.value:
            await self._project_analyzer_invoked(event)
        elif event_type == EventType.ANALYZER_COMPLETED.value:
            await self._project_analyzer_completed(event)
        elif event_type == EventType.PHASE_CHANGED.value:
            await self._project_phase_changed(event)
        elif event_type == EventType.HUMAN_REVIEW_REQUESTED.value:
            await self._project_human_review_requested(event)
        elif event_type == EventType.HUMAN_DECISION_RECEIVED.value:
            await self._project_human_decision_received(event)

        logger.debug(
            "Event projected",
            event_id=str(event.id),
            event_type=event_type,
        )

    async def _get_or_create_investigation(
        self, aggregate_id: UUID
    ) -> InvestigationReadModel:
        """Get or create an investigation read model."""
        stmt = select(InvestigationReadModel).where(
            InvestigationReadModel.id == aggregate_id
        )
        result = await self.session.execute(stmt)
        investigation = result.scalar_one_or_none()

        if investigation is None:
            investigation = InvestigationReadModel(id=aggregate_id)
            self.session.add(investigation)

        return investigation

    async def _get_or_create_hourly_metrics(
        self, timestamp: datetime
    ) -> MetricsHourly:
        """Get or create hourly metrics for a given timestamp."""
        hour = timestamp.replace(minute=0, second=0, microsecond=0)

        stmt = select(MetricsHourly).where(MetricsHourly.hour == hour)
        result = await self.session.execute(stmt)
        metrics = result.scalar_one_or_none()

        if metrics is None:
            metrics = MetricsHourly(hour=hour)
            self.session.add(metrics)

        return metrics

    async def _get_or_create_ioc_stats(
        self, value: str, ioc_type: str
    ) -> IOCStats:
        """Get or create IOC statistics."""
        stmt = select(IOCStats).where(
            IOCStats.value == value,
            IOCStats.type == ioc_type,
        )
        result = await self.session.execute(stmt)
        stats = result.scalar_one_or_none()

        if stats is None:
            stats = IOCStats(value=value, type=ioc_type)
            self.session.add(stats)

        return stats

    async def _get_or_create_rule_stats(self, rule_id: str) -> RuleStats:
        """Get or create rule statistics."""
        stmt = select(RuleStats).where(RuleStats.rule_id == rule_id)
        result = await self.session.execute(stmt)
        stats = result.scalar_one_or_none()

        if stats is None:
            stats = RuleStats(rule_id=rule_id)
            self.session.add(stats)

        return stats

    async def _get_or_create_analyzer_stats(
        self, analyzer: str
    ) -> AnalyzerStats:
        """Get or create analyzer statistics."""
        stmt = select(AnalyzerStats).where(AnalyzerStats.analyzer == analyzer)
        result = await self.session.execute(stmt)
        stats = result.scalar_one_or_none()

        if stats is None:
            stats = AnalyzerStats(analyzer=analyzer)
            self.session.add(stats)

        return stats

    # =========================================================================
    # Investigation Lifecycle Projections
    # =========================================================================

    async def _project_investigation_created(self, event: Event) -> None:
        """Project INVESTIGATION_CREATED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.created_at = event.timestamp
        investigation.updated_at = event.timestamp
        investigation.status = "pending"
        investigation.phase = "triage"

        # Set title from event data
        if "title" in event.data:
            investigation.title = event.data["title"]

        # Set max severity from event data
        if "max_severity" in event.data:
            investigation.max_severity = event.data["max_severity"]

        # Update hourly metrics
        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.investigations_created += 1

    async def _project_investigation_started(self, event: Event) -> None:
        """Project INVESTIGATION_STARTED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "in_progress"
        investigation.updated_at = event.timestamp

        # Extract title if present
        if "title" in event.data:
            investigation.title = event.data["title"]

    async def _project_investigation_paused(self, event: Event) -> None:
        """Project INVESTIGATION_PAUSED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "paused"
        investigation.updated_at = event.timestamp

    async def _project_investigation_resumed(self, event: Event) -> None:
        """Project INVESTIGATION_RESUMED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "in_progress"
        investigation.updated_at = event.timestamp

    async def _project_investigation_cancelled(self, event: Event) -> None:
        """Project INVESTIGATION_CANCELLED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "cancelled"
        investigation.closed_at = event.timestamp
        investigation.updated_at = event.timestamp
        investigation.phase = "closed"

        if investigation.time_to_triage_seconds is None and investigation.created_at:
            delta = event.timestamp - investigation.created_at
            investigation.time_to_triage_seconds = int(delta.total_seconds())

        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.investigations_closed += 1

    async def _project_alert_correlated(self, event: Event) -> None:
        """Project ALERT_CORRELATED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.alert_count += 1
        investigation.updated_at = event.timestamp

        # Update max severity
        severity = event.data.get("severity")
        if severity:
            if investigation.max_severity is None:
                investigation.max_severity = severity
            elif self._compare_severity(severity, investigation.max_severity) > 0:
                investigation.max_severity = severity

        # Update hourly metrics
        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.total_alerts += 1

        # Update rule stats if rule_id present
        rule_id = event.data.get("rule_id")
        if rule_id:
            rule_stats = await self._get_or_create_rule_stats(rule_id)
            rule_stats.times_triggered += 1

    async def _project_observable_extracted(self, event: Event) -> None:
        """Project OBSERVABLE_EXTRACTED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.observable_count += 1
        investigation.updated_at = event.timestamp

        # Update hourly metrics
        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.total_observables += 1

        # Update IOC stats
        observable_type = event.data.get("type", "unknown")
        observable_value = event.data.get("value", "")
        if observable_value:
            ioc_stats = await self._get_or_create_ioc_stats(
                observable_value, observable_type
            )
            ioc_stats.times_seen += 1
            ioc_stats.last_seen = event.timestamp

    async def _project_enrichment_completed(self, event: Event) -> None:
        """Project ENRICHMENT_COMPLETED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.updated_at = event.timestamp

        # Check if malicious
        is_malicious = event.data.get("is_malicious", False)
        if is_malicious:
            investigation.malicious_count += 1

            # Update IOC stats
            observable_type = event.data.get("observable_type", "unknown")
            observable_value = event.data.get("observable_value", "")
            if observable_value:
                ioc_stats = await self._get_or_create_ioc_stats(
                    observable_value, observable_type
                )
                ioc_stats.malicious_count += 1

                # Add threat actor if present
                threat_actor = event.data.get("threat_actor")
                if threat_actor and threat_actor not in ioc_stats.threat_actors:
                    ioc_stats.threat_actors = ioc_stats.threat_actors + [threat_actor]

            # Update hourly metrics
            metrics = await self._get_or_create_hourly_metrics(event.timestamp)
            metrics.malicious_observables += 1
        else:
            # Update benign count for IOC
            observable_type = event.data.get("observable_type", "unknown")
            observable_value = event.data.get("observable_value", "")
            if observable_value:
                ioc_stats = await self._get_or_create_ioc_stats(
                    observable_value, observable_type
                )
                ioc_stats.benign_count += 1

    async def _project_verdict_rendered(self, event: Event) -> None:
        """Project VERDICT_RENDERED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.updated_at = event.timestamp
        investigation.phase = "verdict"
        investigation.verdict_decision = event.data.get("decision")
        investigation.verdict_confidence = event.data.get("confidence")

        # Calculate time to verdict
        if investigation.created_at:
            delta = event.timestamp - investigation.created_at
            investigation.time_to_verdict_seconds = int(delta.total_seconds())

            # Update hourly metrics with average
            metrics = await self._get_or_create_hourly_metrics(event.timestamp)
            if metrics.avg_time_to_verdict_seconds is None:
                metrics.avg_time_to_verdict_seconds = investigation.time_to_verdict_seconds
            else:
                # Simple moving average
                closed = metrics.investigations_closed + 1
                metrics.avg_time_to_verdict_seconds = int(
                    (metrics.avg_time_to_verdict_seconds * metrics.investigations_closed
                     + investigation.time_to_verdict_seconds) / closed
                )

    async def _project_investigation_escalated(self, event: Event) -> None:
        """Project INVESTIGATION_ESCALATED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "escalated"
        investigation.phase = "escalation"
        investigation.updated_at = event.timestamp

        # Update hourly metrics
        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.escalations += 1

        # Update rule stats for escalation
        rule_id = event.data.get("trigger_rule_id")
        if rule_id:
            rule_stats = await self._get_or_create_rule_stats(rule_id)
            rule_stats.escalation_count += 1

    async def _project_investigation_auto_closed(self, event: Event) -> None:
        """Project INVESTIGATION_AUTO_CLOSED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.status = "auto_closed"
        investigation.closed_at = event.timestamp
        investigation.updated_at = event.timestamp
        investigation.phase = "closed"

        # Update hourly metrics
        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.auto_closed += 1
        metrics.investigations_closed += 1

        # Update rule stats
        rule_id = event.data.get("trigger_rule_id")
        if rule_id:
            rule_stats = await self._get_or_create_rule_stats(rule_id)
            rule_stats.auto_close_count += 1

    async def _project_investigation_closed(self, event: Event) -> None:
        """Project INVESTIGATION_CLOSED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)

        resolution = (event.data.get("resolution") or "").lower()
        verdict_decision = (event.data.get("verdict_decision") or "").lower()
        thehive_case_id = event.data.get("thehive_case_id")
        if thehive_case_id:
            investigation.thehive_case_id = thehive_case_id
        if verdict_decision:
            investigation.verdict_decision = verdict_decision

        if investigation.thehive_case_id:
            investigation.status = "escalated"
        elif "rejected" in resolution:
            investigation.status = "rejected"
        elif verdict_decision == "close" and "closed by ai verdict" in resolution:
            investigation.status = "auto_closed"
        else:
            investigation.status = "closed"

        investigation.closed_at = event.timestamp
        investigation.updated_at = event.timestamp
        investigation.phase = "closed"

        # Calculate time to triage if not already set
        if investigation.time_to_triage_seconds is None and investigation.created_at:
            delta = event.timestamp - investigation.created_at
            investigation.time_to_triage_seconds = int(delta.total_seconds())

        # Update hourly metrics
        if investigation.status != "escalated":
            metrics = await self._get_or_create_hourly_metrics(event.timestamp)
            metrics.investigations_closed += 1
            if investigation.status == "auto_closed":
                metrics.auto_closed += 1

    async def _project_thehive_case_created(self, event: Event) -> None:
        """Project THEHIVE_CASE_CREATED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.thehive_case_id = event.data.get("case_id")
        investigation.status = "escalated"
        investigation.phase = "escalation"
        investigation.updated_at = event.timestamp

        metrics = await self._get_or_create_hourly_metrics(event.timestamp)
        metrics.escalations += 1

    async def _project_phase_changed(self, event: Event) -> None:
        """Project PHASE_CHANGED event."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        new_phase = (
            event.data.get("new_phase")
            or event.data.get("to_phase")
            or event.data.get("phase")
        )
        if new_phase:
            investigation.phase = new_phase
        investigation.updated_at = event.timestamp

        # Calculate time to triage when entering verdict phase
        if new_phase == "verdict" and investigation.time_to_triage_seconds is None:
            if investigation.created_at:
                delta = event.timestamp - investigation.created_at
                investigation.time_to_triage_seconds = int(delta.total_seconds())

    # =========================================================================
    # Analyzer Projections
    # =========================================================================

    async def _project_analyzer_invoked(self, event: Event) -> None:
        """Project ANALYZER_INVOKED event."""
        analyzer_name = event.data.get("analyzer")
        if analyzer_name:
            stats = await self._get_or_create_analyzer_stats(analyzer_name)
            stats.invocations += 1

    async def _project_analyzer_completed(self, event: Event) -> None:
        """Project ANALYZER_COMPLETED event."""
        analyzer_name = event.data.get("analyzer")
        if analyzer_name:
            stats = await self._get_or_create_analyzer_stats(analyzer_name)

            success = event.data.get("success", True)
            if success:
                stats.successes += 1
            else:
                stats.failures += 1

            # Update average response time
            response_time_ms = event.data.get("response_time_ms")
            if response_time_ms is not None:
                if stats.avg_response_time_ms is None:
                    stats.avg_response_time_ms = float(response_time_ms)
                else:
                    total_calls = stats.successes + stats.failures
                    stats.avg_response_time_ms = (
                        (stats.avg_response_time_ms * (total_calls - 1)
                         + response_time_ms) / total_calls
                    )

    # =========================================================================
    # Human Review Projections
    # =========================================================================

    async def _project_human_review_requested(self, event: Event) -> None:
        """Project HUMAN_REVIEW_REQUESTED event - create pending review."""
        investigation = await self._get_or_create_investigation(event.aggregate_id)
        investigation.phase = "human_review"
        if investigation.status == "pending":
            investigation.status = "in_progress"
        investigation.updated_at = event.timestamp

        # Check if a pending review already exists
        existing = await self.session.execute(
            select(PendingReview).where(
                PendingReview.investigation_id == event.aggregate_id,
                PendingReview.status == "pending",
            )
        )
        if existing.scalar_one_or_none():
            # Already exists, skip
            return

        # Create pending review
        pending_review = PendingReview(
            investigation_id=event.aggregate_id,
            status="pending",
            title=investigation.title or "Untitled Investigation",
            description=event.data.get("reason", "Requires human review"),
            max_severity=investigation.max_severity or "medium",
            alert_count=investigation.alert_count or 0,
            malicious_count=investigation.malicious_count or 0,
            suspicious_count=investigation.suspicious_count or 0,
            clean_count=investigation.clean_count or 0,
            ai_decision=event.data.get("verdict_decision"),
            ai_confidence=event.data.get("verdict_confidence"),
            created_at=event.timestamp,
            expires_at=None,  # No automatic expiration
        )
        self.session.add(pending_review)
        await self.session.flush()  # Ensure the record is persisted

        logger.info(
            "pending_review_created",
            investigation_id=str(event.aggregate_id),
            title=investigation.title,
        )

    async def _project_human_decision_received(self, event: Event) -> None:
        """Project HUMAN_DECISION_RECEIVED event - update pending review status."""
        # Find and update pending review
        result = await self.session.execute(
            select(PendingReview).where(
                PendingReview.investigation_id == event.aggregate_id,
                PendingReview.status == "pending",
            )
        )
        pending_review = result.scalar_one_or_none()

        if pending_review:
            decision = event.data.get("decision", "unknown")
            if decision == "approve":
                pending_review.status = "approved"
            elif decision == "reject":
                pending_review.status = "rejected"
            elif decision == "more_info":
                pending_review.status = "info_requested"
            else:
                pending_review.status = decision

            pending_review.responded_at = event.timestamp
            if "reviewer" in event.data:
                pending_review.reviewer = event.data.get("reviewer")
            if "feedback" in event.data:
                pending_review.feedback = event.data.get("feedback")

            logger.info(
                "pending_review_updated",
                investigation_id=str(event.aggregate_id),
                status=pending_review.status,
            )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    @staticmethod
    def _compare_severity(s1: str, s2: str) -> int:
        """Compare two severity strings.

        Returns:
            -1 if s1 < s2, 0 if equal, 1 if s1 > s2
        """
        severity_order = {
            "low": 1,
            "medium": 2,
            "high": 3,
            "critical": 4,
        }
        v1 = severity_order.get(s1.lower(), 0)
        v2 = severity_order.get(s2.lower(), 0)

        if v1 < v2:
            return -1
        elif v1 > v2:
            return 1
        return 0


class ProjectingEventStore:
    """EventStore wrapper that automatically projects events on append.

    This provides a convenient way to ensure events are always projected
    within the same transaction.
    """

    def __init__(self, session: AsyncSession):
        """Initialize with a database session.

        Args:
            session: Async SQLAlchemy session for database operations
        """
        from soctalk.persistence.store import EventStore

        self.session = session
        self.event_store = EventStore(session)
        self.projector = Projector(session)

    async def append(
        self,
        aggregate_id: UUID,
        event_type: EventType | str,
        data: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        aggregate_type: str = "Investigation",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        """Append an event and project it to read models.

        Args:
            aggregate_id: ID of the aggregate this event belongs to
            event_type: Type of the event
            data: Event payload data
            metadata: Optional metadata (actor, correlation_id, etc.)
            aggregate_type: Type of aggregate (default: "Investigation")
            expected_version: For optimistic concurrency - expected current version
            idempotency_key: Optional key for idempotent operations

        Returns:
            The created Event
        """
        event = await self.event_store.append(
            aggregate_id=aggregate_id,
            event_type=event_type,
            data=data,
            metadata=metadata,
            aggregate_type=aggregate_type,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

        # Project within the same transaction
        await self.projector.project(event)

        return event

    async def append_batch(
        self,
        aggregate_id: UUID,
        events: list[tuple[EventType | str, dict[str, Any], dict[str, Any] | None]],
        aggregate_type: str = "Investigation",
        expected_version: int | None = None,
    ) -> list[Event]:
        """Append multiple events atomically and project them.

        Args:
            aggregate_id: ID of the aggregate
            events: List of (event_type, data, metadata) tuples
            aggregate_type: Type of aggregate
            expected_version: Expected current version for optimistic concurrency

        Returns:
            List of created Events
        """
        created_events = await self.event_store.append_batch(
            aggregate_id=aggregate_id,
            events=events,
            aggregate_type=aggregate_type,
            expected_version=expected_version,
        )

        # Project all events within the same transaction
        for event in created_events:
            await self.projector.project(event)

        return created_events

    # Delegate read operations to the underlying EventStore
    async def get_events(
        self,
        aggregate_id: UUID,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> list[Event]:
        """Get events for an aggregate."""
        return await self.event_store.get_events(
            aggregate_id, from_version, to_version
        )

    async def get_events_by_type(
        self,
        event_type: EventType | str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Get events by type across all aggregates."""
        return await self.event_store.get_events_by_type(event_type, since, limit)

    async def get_latest_version(self, aggregate_id: UUID) -> int:
        """Get the latest version number for an aggregate."""
        return await self.event_store.get_latest_version(aggregate_id)
