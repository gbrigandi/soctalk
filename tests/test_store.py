"""Unit tests for EventStore."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from soctalk.persistence.events import EventType
from soctalk.persistence.models import Event
from soctalk.persistence.store import ConcurrencyError, EventStore, IdempotencyError


class TestEventStore:
    """Tests for EventStore class."""

    @pytest.fixture
    def event_store(self, mock_session: AsyncMock) -> EventStore:
        """Create an EventStore instance with mock session."""
        return EventStore(mock_session)

    async def test_append_creates_event_with_correct_fields(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test that append creates an event with all expected fields."""
        # Setup: mock get_latest_version to return 0 (no existing events)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        data = {"alert_id": "12345", "severity": "high"}
        metadata = {"actor": "system", "correlation_id": "abc123"}

        event = await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type=EventType.ALERT_CORRELATED,
            data=data,
            metadata=metadata,
        )

        # Verify event was added to session
        mock_session.add.assert_called_once()
        added_event = mock_session.add.call_args[0][0]

        assert added_event.aggregate_id == sample_aggregate_id
        assert added_event.event_type == EventType.ALERT_CORRELATED.value
        assert added_event.version == 1
        assert added_event.data == data
        assert added_event.event_metadata == metadata
        assert added_event.aggregate_type == "Investigation"

    async def test_append_with_string_event_type(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test that append works with string event type."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        event = await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type="custom.event",
            data={},
        )

        added_event = mock_session.add.call_args[0][0]
        assert added_event.event_type == "custom.event"

    async def test_append_increments_version(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test that append correctly increments version number."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 5  # Current version is 5
        mock_session.execute.return_value = mock_result

        await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )

        added_event = mock_session.add.call_args[0][0]
        assert added_event.version == 6

    async def test_append_with_expected_version_success(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test optimistic concurrency check passes when versions match."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 3
        mock_session.execute.return_value = mock_result

        # Should not raise when expected_version matches current
        await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
            expected_version=3,
        )

        mock_session.add.assert_called_once()

    async def test_append_with_expected_version_raises_concurrency_error(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test optimistic concurrency check fails when versions mismatch."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 5  # Current version is 5
        mock_session.execute.return_value = mock_result

        with pytest.raises(ConcurrencyError) as exc_info:
            await event_store.append(
                aggregate_id=sample_aggregate_id,
                event_type=EventType.INVESTIGATION_CREATED,
                data={},
                expected_version=3,  # Expected 3, but current is 5
            )

        assert exc_info.value.aggregate_id == sample_aggregate_id
        assert exc_info.value.expected_version == 3
        assert exc_info.value.actual_version == 5

    async def test_append_with_idempotency_key_returns_existing(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
        sample_event: Event,
    ):
        """Test idempotency returns existing event when key matches."""
        # First call returns version, second returns existing event
        mock_version_result = MagicMock()
        mock_version_result.scalar_one_or_none.return_value = 0

        mock_event_result = MagicMock()
        mock_event_result.scalar_one_or_none.return_value = sample_event

        mock_session.execute.side_effect = [mock_version_result, mock_event_result]

        result = await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
            idempotency_key="unique-key-123",
        )

        assert result == sample_event
        mock_session.add.assert_not_called()

    async def test_append_with_new_idempotency_key_creates_event(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test idempotency creates new event when key is new."""
        mock_version_result = MagicMock()
        mock_version_result.scalar_one_or_none.return_value = 0

        mock_event_result = MagicMock()
        mock_event_result.scalar_one_or_none.return_value = None  # No existing event

        mock_session.execute.side_effect = [mock_version_result, mock_event_result]

        await event_store.append(
            aggregate_id=sample_aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
            idempotency_key="new-unique-key",
        )

        mock_session.add.assert_called_once()
        added_event = mock_session.add.call_args[0][0]
        assert added_event.idempotency_key == "new-unique-key"

    async def test_append_handles_integrity_error_version_conflict(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test handling of IntegrityError for version conflict."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 0
        mock_session.execute.return_value = mock_result

        # Simulate IntegrityError on flush
        mock_session.flush.side_effect = IntegrityError(
            "duplicate key", {}, Exception("uq_aggregate_version")
        )

        # After rollback, get_latest_version returns the actual version
        async def execute_side_effect(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = 1
            return result

        mock_session.execute.side_effect = [mock_result, MagicMock(scalar_one_or_none=MagicMock(return_value=1))]

        with pytest.raises(ConcurrencyError):
            await event_store.append(
                aggregate_id=sample_aggregate_id,
                event_type=EventType.INVESTIGATION_CREATED,
                data={},
            )

        mock_session.rollback.assert_called_once()


class TestEventStoreAppendBatch:
    """Tests for EventStore.append_batch method."""

    @pytest.fixture
    def event_store(self, mock_session: AsyncMock) -> EventStore:
        """Create an EventStore instance with mock session."""
        return EventStore(mock_session)

    async def test_append_batch_creates_multiple_events(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test batch append creates multiple events with correct versions."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 0
        mock_session.execute.return_value = mock_result

        events = [
            (EventType.INVESTIGATION_CREATED, {"title": "Test"}, None),
            (EventType.ALERT_CORRELATED, {"alert_id": "1"}, {"actor": "system"}),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "ip", "value": "1.2.3.4"}, None),
        ]

        result = await event_store.append_batch(
            aggregate_id=sample_aggregate_id,
            events=events,
        )

        assert len(result) == 3
        assert mock_session.add.call_count == 3

        # Verify versions are sequential
        calls = mock_session.add.call_args_list
        assert calls[0][0][0].version == 1
        assert calls[1][0][0].version == 2
        assert calls[2][0][0].version == 3

    async def test_append_batch_with_expected_version(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test batch append respects expected_version."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 5
        mock_session.execute.return_value = mock_result

        events = [
            (EventType.PHASE_CHANGED, {"new_phase": "enrichment"}, None),
        ]

        await event_store.append_batch(
            aggregate_id=sample_aggregate_id,
            events=events,
            expected_version=5,
        )

        added_event = mock_session.add.call_args[0][0]
        assert added_event.version == 6

    async def test_append_batch_raises_concurrency_error(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test batch append raises ConcurrencyError on version mismatch."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 10
        mock_session.execute.return_value = mock_result

        events = [
            (EventType.INVESTIGATION_CREATED, {}, None),
        ]

        with pytest.raises(ConcurrencyError) as exc_info:
            await event_store.append_batch(
                aggregate_id=sample_aggregate_id,
                events=events,
                expected_version=5,
            )

        assert exc_info.value.expected_version == 5
        assert exc_info.value.actual_version == 10


class TestEventStoreQueries:
    """Tests for EventStore query methods."""

    @pytest.fixture
    def event_store(self, mock_session: AsyncMock) -> EventStore:
        """Create an EventStore instance with mock session."""
        return EventStore(mock_session)

    async def test_get_events_returns_ordered_events(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test get_events returns events in version order."""
        events = [
            Event(
                id=uuid4(),
                aggregate_id=sample_aggregate_id,
                event_type="test",
                version=i,
                timestamp=datetime.utcnow(),
                data={},
                event_metadata={},
            )
            for i in range(1, 4)
        ]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute.return_value = mock_result

        result = await event_store.get_events(sample_aggregate_id)

        assert len(result) == 3
        mock_session.execute.assert_called_once()

    async def test_get_events_with_version_range(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test get_events respects from_version and to_version."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        await event_store.get_events(
            sample_aggregate_id,
            from_version=5,
            to_version=10,
        )

        # Verify the query was executed (detailed query verification would need SQL inspection)
        mock_session.execute.assert_called_once()

    async def test_get_events_by_type(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
    ):
        """Test get_events_by_type filters by event type."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        await event_store.get_events_by_type(
            EventType.VERDICT_RENDERED,
            limit=50,
        )

        mock_session.execute.assert_called_once()

    async def test_get_events_by_type_with_since(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
    ):
        """Test get_events_by_type respects since parameter."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        since = datetime.utcnow() - timedelta(hours=1)
        await event_store.get_events_by_type(
            EventType.ALERT_CORRELATED,
            since=since,
        )

        mock_session.execute.assert_called_once()

    async def test_get_latest_version_returns_version(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test get_latest_version returns correct version."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = 42
        mock_session.execute.return_value = mock_result

        version = await event_store.get_latest_version(sample_aggregate_id)

        assert version == 42

    async def test_get_latest_version_returns_zero_when_no_events(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
        sample_aggregate_id: UUID,
    ):
        """Test get_latest_version returns 0 for new aggregates."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        version = await event_store.get_latest_version(sample_aggregate_id)

        assert version == 0

    async def test_get_all_aggregate_ids(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
    ):
        """Test get_all_aggregate_ids returns distinct IDs."""
        ids = [uuid4() for _ in range(3)]
        mock_result = MagicMock()
        mock_result.all.return_value = [(id,) for id in ids]
        mock_session.execute.return_value = mock_result

        result = await event_store.get_all_aggregate_ids()

        assert len(result) == 3
        assert all(id in result for id in ids)

    async def test_get_events_since(
        self,
        event_store: EventStore,
        mock_session: AsyncMock,
    ):
        """Test get_events_since filters by timestamp."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        since = datetime.utcnow() - timedelta(minutes=30)
        await event_store.get_events_since(
            since=since,
            event_types=[EventType.ALERT_CORRELATED, EventType.OBSERVABLE_EXTRACTED],
            limit=500,
        )

        mock_session.execute.assert_called_once()


class TestConcurrencyError:
    """Tests for ConcurrencyError exception."""

    def test_error_message_format(self):
        """Test error message includes all relevant information."""
        aggregate_id = uuid4()
        error = ConcurrencyError(aggregate_id, expected_version=5, actual_version=8)

        assert str(aggregate_id) in str(error)
        assert "5" in str(error)
        assert "8" in str(error)

    def test_error_attributes(self):
        """Test error has correct attributes."""
        aggregate_id = uuid4()
        error = ConcurrencyError(aggregate_id, expected_version=3, actual_version=7)

        assert error.aggregate_id == aggregate_id
        assert error.expected_version == 3
        assert error.actual_version == 7


class TestIdempotencyError:
    """Tests for IdempotencyError exception."""

    def test_error_message_format(self):
        """Test error message includes idempotency key."""
        error = IdempotencyError("my-unique-key")

        assert "my-unique-key" in str(error)

    def test_error_attributes(self):
        """Test error has correct attributes."""
        error = IdempotencyError("test-key-123")

        assert error.idempotency_key == "test-key-123"
