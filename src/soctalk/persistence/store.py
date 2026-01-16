"""Event store for append-only event storage with idempotency and optimistic concurrency."""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.persistence.events import EventType
from soctalk.persistence.models import Event

logger = structlog.get_logger()


class ConcurrencyError(Exception):
    """Raised when optimistic concurrency check fails."""

    def __init__(self, aggregate_id: UUID, expected_version: int, actual_version: int):
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict for aggregate {aggregate_id}: "
            f"expected version {expected_version}, actual version {actual_version}"
        )


class IdempotencyError(Exception):
    """Raised when an idempotency key has already been used."""

    def __init__(self, idempotency_key: str):
        self.idempotency_key = idempotency_key
        super().__init__(f"Event with idempotency key '{idempotency_key}' already exists")


class EventStore:
    """Append-only event store with idempotency and optimistic concurrency support."""

    def __init__(self, session: AsyncSession):
        """Initialize the event store with a database session.

        Args:
            session: Async SQLAlchemy session for database operations
        """
        self.session = session

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
        """Append a new event to the store.

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

        Raises:
            ConcurrencyError: If expected_version doesn't match current version
            IdempotencyError: If idempotency_key already exists
        """
        # Convert enum to string if needed
        event_type_str = event_type.value if isinstance(event_type, EventType) else event_type

        # Get current version for this aggregate
        current_version = await self.get_latest_version(aggregate_id)

        # Optimistic concurrency check
        if expected_version is not None and current_version != expected_version:
            raise ConcurrencyError(aggregate_id, expected_version, current_version)

        new_version = current_version + 1

        # Check idempotency key if provided
        if idempotency_key:
            existing = await self._get_by_idempotency_key(idempotency_key)
            if existing:
                logger.info(
                    "Idempotent event already exists",
                    idempotency_key=idempotency_key,
                    event_id=str(existing.id),
                )
                return existing

        # Create the event
        event = Event(
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            event_type=event_type_str,
            version=new_version,
            timestamp=datetime.utcnow(),
            data=data,
            event_metadata=metadata or {},
            idempotency_key=idempotency_key,
        )

        try:
            self.session.add(event)
            await self.session.flush()
            logger.info(
                "Event appended",
                event_id=str(event.id),
                aggregate_id=str(aggregate_id),
                event_type=event_type_str,
                version=new_version,
            )
            return event
        except IntegrityError as e:
            await self.session.rollback()
            # Check if it's a version conflict
            if "uq_aggregate_version" in str(e):
                actual_version = await self.get_latest_version(aggregate_id)
                raise ConcurrencyError(aggregate_id, new_version - 1, actual_version) from e
            # Check if it's an idempotency conflict
            if "ix_events_idempotency_key" in str(e) and idempotency_key:
                raise IdempotencyError(idempotency_key) from e
            raise

    async def append_batch(
        self,
        aggregate_id: UUID,
        events: list[tuple[EventType | str, dict[str, Any], dict[str, Any] | None]],
        aggregate_type: str = "Investigation",
        expected_version: int | None = None,
    ) -> list[Event]:
        """Append multiple events atomically.

        Args:
            aggregate_id: ID of the aggregate
            events: List of (event_type, data, metadata) tuples
            aggregate_type: Type of aggregate
            expected_version: Expected current version for optimistic concurrency

        Returns:
            List of created Events
        """
        current_version = await self.get_latest_version(aggregate_id)

        if expected_version is not None and current_version != expected_version:
            raise ConcurrencyError(aggregate_id, expected_version, current_version)

        created_events = []
        for i, (event_type, data, metadata) in enumerate(events):
            event_type_str = event_type.value if isinstance(event_type, EventType) else event_type
            new_version = current_version + i + 1

            event = Event(
                aggregate_id=aggregate_id,
                aggregate_type=aggregate_type,
                event_type=event_type_str,
                version=new_version,
                timestamp=datetime.utcnow(),
                data=data,
                event_metadata=metadata or {},
            )
            self.session.add(event)
            created_events.append(event)

        await self.session.flush()
        return created_events

    async def get_events(
        self,
        aggregate_id: UUID,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> list[Event]:
        """Get events for an aggregate.

        Args:
            aggregate_id: ID of the aggregate
            from_version: Start from this version (inclusive)
            to_version: Up to this version (inclusive)

        Returns:
            List of events ordered by version
        """
        stmt = select(Event).where(Event.aggregate_id == aggregate_id)

        if from_version is not None:
            stmt = stmt.where(Event.version >= from_version)
        if to_version is not None:
            stmt = stmt.where(Event.version <= to_version)

        stmt = stmt.order_by(Event.version)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_events_by_type(
        self,
        event_type: EventType | str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Get events by type across all aggregates.

        Args:
            event_type: Type of events to retrieve
            since: Only events after this timestamp
            limit: Maximum number of events to return

        Returns:
            List of events ordered by timestamp descending
        """
        event_type_str = event_type.value if isinstance(event_type, EventType) else event_type
        stmt = select(Event).where(Event.event_type == event_type_str)

        if since:
            stmt = stmt.where(Event.timestamp > since)

        stmt = stmt.order_by(Event.timestamp.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_version(self, aggregate_id: UUID) -> int:
        """Get the latest version number for an aggregate.

        Args:
            aggregate_id: ID of the aggregate

        Returns:
            Latest version number, or 0 if no events exist
        """
        stmt = (
            select(Event.version)
            .where(Event.aggregate_id == aggregate_id)
            .order_by(Event.version.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        version = result.scalar_one_or_none()
        return version or 0

    async def get_all_aggregate_ids(
        self,
        aggregate_type: str = "Investigation",
        limit: int = 1000,
    ) -> list[UUID]:
        """Get all aggregate IDs of a given type.

        Args:
            aggregate_type: Type of aggregates to retrieve
            limit: Maximum number of IDs to return

        Returns:
            List of aggregate IDs
        """
        stmt = (
            select(Event.aggregate_id)
            .where(Event.aggregate_type == aggregate_type)
            .distinct()
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def _get_by_idempotency_key(self, idempotency_key: str) -> Event | None:
        """Get an event by its idempotency key.

        Args:
            idempotency_key: The idempotency key to search for

        Returns:
            The event if found, None otherwise
        """
        stmt = select(Event).where(Event.idempotency_key == idempotency_key)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_events_since(
        self,
        since: datetime,
        event_types: list[EventType | str] | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        """Get events since a given timestamp.

        Args:
            since: Only events after this timestamp
            event_types: Filter by event types (optional)
            limit: Maximum number of events to return

        Returns:
            List of events ordered by timestamp
        """
        stmt = select(Event).where(Event.timestamp > since)

        if event_types:
            type_strs = [
                t.value if isinstance(t, EventType) else t for t in event_types
            ]
            stmt = stmt.where(Event.event_type.in_(type_strs))

        stmt = stmt.order_by(Event.timestamp).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
