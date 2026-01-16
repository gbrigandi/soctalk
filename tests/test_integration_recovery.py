"""Integration tests for event store recovery flow.

These tests require a PostgreSQL database running via Docker.
Run: docker compose -f docker-compose.test.yml up -d
"""

import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from soctalk.persistence.events import EventType
from soctalk.persistence.models import (
    AnalyzerStats,
    Event,
    InvestigationReadModel,
    IOCStats,
    MetricsHourly,
    RuleStats,
)
from soctalk.persistence.projector import Projector, ProjectingEventStore
from soctalk.persistence.store import ConcurrencyError, EventStore


# Test database URL - use environment variable or default for docker-compose.test.yml
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://soctalk_test:soctalk_test@localhost:5433/soctalk_test",
)


@pytest_asyncio.fixture
async def clean_session():
    """Create a clean database session with fresh schema."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, pool_pre_ping=True)

    async with engine.begin() as conn:
        # Completely reset the public schema to avoid index conflicts
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO soctalk_test"))
        await conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.mark.integration
class TestEventStoreIntegration:
    """Integration tests for EventStore with real PostgreSQL."""

    async def test_append_and_retrieve_event(self, clean_session):
        """Test appending and retrieving an event."""
        store = EventStore(clean_session)
        aggregate_id = uuid4()

        event = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={"title": "Test Investigation"},
            metadata={"actor": "test"},
        )
        await clean_session.commit()

        # Retrieve events
        events = await store.get_events(aggregate_id)
        assert len(events) == 1
        assert events[0].event_type == EventType.INVESTIGATION_CREATED.value
        assert events[0].data["title"] == "Test Investigation"
        assert events[0].version == 1

    async def test_version_sequencing(self, clean_session):
        """Test that versions are correctly sequenced."""
        store = EventStore(clean_session)
        aggregate_id = uuid4()

        # Append multiple events
        await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )
        await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_STARTED,
            data={"title": "Test"},
        )
        await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.ALERT_CORRELATED,
            data={"alert_id": "1", "severity": "high"},
        )
        await clean_session.commit()

        events = await store.get_events(aggregate_id)
        assert len(events) == 3
        assert [e.version for e in events] == [1, 2, 3]

    async def test_optimistic_concurrency(self, clean_session):
        """Test optimistic concurrency control."""
        store = EventStore(clean_session)
        aggregate_id = uuid4()

        # Create initial event
        await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )
        await clean_session.commit()

        # Try to append with wrong expected version
        with pytest.raises(ConcurrencyError) as exc_info:
            await store.append(
                aggregate_id=aggregate_id,
                event_type=EventType.INVESTIGATION_STARTED,
                data={},
                expected_version=0,  # Wrong - should be 1
            )

        assert exc_info.value.expected_version == 0
        assert exc_info.value.actual_version == 1

    async def test_idempotency(self, clean_session):
        """Test idempotent event creation."""
        store = EventStore(clean_session)
        aggregate_id = uuid4()
        idempotency_key = "unique-key-12345"

        # First append
        event1 = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={"title": "Test"},
            idempotency_key=idempotency_key,
        )
        await clean_session.commit()

        # Second append with same key should return same event
        event2 = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={"title": "Different"},  # Different data
            idempotency_key=idempotency_key,
        )

        assert event1.id == event2.id
        assert event2.data["title"] == "Test"  # Original data preserved

    async def test_batch_append(self, clean_session):
        """Test batch event append."""
        store = EventStore(clean_session)
        aggregate_id = uuid4()

        events = [
            (EventType.INVESTIGATION_CREATED, {"title": "Test"}, None),
            (EventType.ALERT_CORRELATED, {"alert_id": "1", "severity": "high"}, None),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "ip", "value": "1.2.3.4"}, None),
        ]

        created = await store.append_batch(aggregate_id, events)
        await clean_session.commit()

        assert len(created) == 3
        assert [e.version for e in created] == [1, 2, 3]

        # Verify retrieval
        stored = await store.get_events(aggregate_id)
        assert len(stored) == 3


@pytest.mark.integration
class TestProjectorIntegration:
    """Integration tests for Projector with real PostgreSQL."""

    async def test_project_investigation_lifecycle(self, clean_session):
        """Test projecting investigation lifecycle events."""
        store = EventStore(clean_session)
        projector = Projector(clean_session)
        aggregate_id = uuid4()

        # Create event
        event1 = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )
        await projector.project(event1)
        await clean_session.commit()

        # Verify read model
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        investigation = result.scalar_one()
        assert investigation.status == "pending"
        assert investigation.phase == "triage"

        # Start investigation
        event2 = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_STARTED,
            data={"title": "Security Incident #1"},
        )
        await projector.project(event2)
        await clean_session.commit()

        await clean_session.refresh(investigation)
        assert investigation.status == "in_progress"
        assert investigation.title == "Security Incident #1"

    async def test_project_alerts_and_observables(self, clean_session):
        """Test projecting alerts and observables."""
        store = EventStore(clean_session)
        projector = Projector(clean_session)
        aggregate_id = uuid4()

        # Create investigation
        e1 = await store.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )
        await projector.project(e1)

        # Add alerts
        for i in range(3):
            e = await store.append(
                aggregate_id=aggregate_id,
                event_type=EventType.ALERT_CORRELATED,
                data={"alert_id": f"alert-{i}", "severity": "medium"},
            )
            await projector.project(e)

        # Add observables
        for i in range(5):
            e = await store.append(
                aggregate_id=aggregate_id,
                event_type=EventType.OBSERVABLE_EXTRACTED,
                data={"type": "ip", "value": f"192.168.1.{i}"},
            )
            await projector.project(e)

        await clean_session.commit()

        # Verify read model
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        investigation = result.scalar_one()
        assert investigation.alert_count == 3
        assert investigation.observable_count == 5


@pytest.mark.integration
class TestRecoveryFlow:
    """Integration tests for the CQRS recovery flow."""

    async def test_full_recovery_from_events(self, clean_session):
        """Test complete recovery of read models from event history.

        This simulates a disaster recovery scenario where read models
        are corrupted or lost and need to be rebuilt from events.
        """
        store = EventStore(clean_session)
        projector = Projector(clean_session)
        aggregate_id = uuid4()

        # Phase 1: Create events and project them
        events_data = [
            (EventType.INVESTIGATION_CREATED, {}),
            (EventType.INVESTIGATION_STARTED, {"title": "Malware Investigation"}),
            (EventType.ALERT_CORRELATED, {"alert_id": "a1", "severity": "high", "rule_id": "100001"}),
            (EventType.ALERT_CORRELATED, {"alert_id": "a2", "severity": "critical", "rule_id": "100002"}),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "ip", "value": "10.0.0.1"}),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "domain", "value": "malware.example.com"}),
            (EventType.ENRICHMENT_COMPLETED, {
                "observable_value": "malware.example.com",
                "observable_type": "domain",
                "is_malicious": True,
                "threat_actor": "APT29",
            }),
            (EventType.PHASE_CHANGED, {"new_phase": "enrichment"}),
            (EventType.VERDICT_RENDERED, {"decision": "malicious", "confidence": 0.95}),
        ]

        for event_type, data in events_data:
            event = await store.append(
                aggregate_id=aggregate_id,
                event_type=event_type,
                data=data,
            )
            await projector.project(event)

        await clean_session.commit()

        # Capture expected state
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        original = result.scalar_one()
        expected_state = {
            "title": original.title,
            "status": original.status,
            "phase": original.phase,
            "alert_count": original.alert_count,
            "observable_count": original.observable_count,
            "malicious_count": original.malicious_count,
            "max_severity": original.max_severity,
            "verdict_decision": original.verdict_decision,
            "verdict_confidence": original.verdict_confidence,
        }

        # Phase 2: Simulate disaster - delete read models
        await clean_session.execute(delete(InvestigationReadModel))
        await clean_session.execute(delete(MetricsHourly))
        await clean_session.execute(delete(IOCStats))
        await clean_session.execute(delete(RuleStats))
        await clean_session.execute(delete(AnalyzerStats))
        await clean_session.commit()

        # Verify read models are gone
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        assert result.scalar_one_or_none() is None

        # Phase 3: Recovery - replay events
        events = await store.get_events(aggregate_id)
        assert len(events) == len(events_data)

        for event in events:
            await projector.project(event)
        await clean_session.commit()

        # Phase 4: Verify recovered state matches original
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        recovered = result.scalar_one()

        assert recovered.title == expected_state["title"]
        assert recovered.status == expected_state["status"]
        assert recovered.phase == expected_state["phase"]
        assert recovered.alert_count == expected_state["alert_count"]
        assert recovered.observable_count == expected_state["observable_count"]
        assert recovered.malicious_count == expected_state["malicious_count"]
        assert recovered.max_severity == expected_state["max_severity"]
        assert recovered.verdict_decision == expected_state["verdict_decision"]
        assert recovered.verdict_confidence == expected_state["verdict_confidence"]

    async def test_partial_recovery_from_checkpoint(self, clean_session):
        """Test partial recovery using version checkpoints."""
        store = EventStore(clean_session)
        projector = Projector(clean_session)
        aggregate_id = uuid4()

        # Create events up to version 5
        for i, event_type in enumerate([
            EventType.INVESTIGATION_CREATED,
            EventType.INVESTIGATION_STARTED,
            EventType.ALERT_CORRELATED,
            EventType.OBSERVABLE_EXTRACTED,
            EventType.PHASE_CHANGED,
        ]):
            data = {}
            if event_type == EventType.INVESTIGATION_STARTED:
                data = {"title": "Test Investigation"}
            elif event_type == EventType.ALERT_CORRELATED:
                data = {"alert_id": f"a{i}", "severity": "medium"}
            elif event_type == EventType.OBSERVABLE_EXTRACTED:
                data = {"type": "ip", "value": f"10.0.0.{i}"}
            elif event_type == EventType.PHASE_CHANGED:
                data = {"new_phase": "enrichment"}

            event = await store.append(
                aggregate_id=aggregate_id,
                event_type=event_type,
                data=data,
            )
            await projector.project(event)

        await clean_session.commit()

        # Record checkpoint at version 5
        version_checkpoint = await store.get_latest_version(aggregate_id)
        assert version_checkpoint == 5

        # Capture state at checkpoint
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        checkpoint_state = result.scalar_one()
        checkpoint_alert_count = checkpoint_state.alert_count

        # Add more events after checkpoint
        new_events = [
            (EventType.ALERT_CORRELATED, {"alert_id": "a6", "severity": "high"}),
            (EventType.ALERT_CORRELATED, {"alert_id": "a7", "severity": "critical"}),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "hash", "value": "abc123"}),
        ]

        for event_type, data in new_events:
            event = await store.append(
                aggregate_id=aggregate_id,
                event_type=event_type,
                data=data,
            )
            await projector.project(event)

        await clean_session.commit()

        # Verify full state
        await clean_session.refresh(checkpoint_state)
        assert checkpoint_state.alert_count == checkpoint_alert_count + 2

        # Simulate partial corruption - reset to checkpoint state manually
        checkpoint_state.alert_count = checkpoint_alert_count
        checkpoint_state.observable_count = 1  # Wrong value
        await clean_session.commit()

        # Recovery: replay only events from checkpoint
        events_from_checkpoint = await store.get_events(
            aggregate_id, from_version=version_checkpoint + 1
        )
        assert len(events_from_checkpoint) == 3

        for event in events_from_checkpoint:
            await projector.project(event)
        await clean_session.commit()

        # Verify recovered state
        await clean_session.refresh(checkpoint_state)
        assert checkpoint_state.alert_count == checkpoint_alert_count + 2

    async def test_multi_aggregate_recovery(self, clean_session):
        """Test recovery across multiple aggregates."""
        store = EventStore(clean_session)
        projector = Projector(clean_session)

        # Create multiple investigations
        aggregate_ids = [uuid4() for _ in range(3)]
        expected_states = {}

        for i, agg_id in enumerate(aggregate_ids):
            events = [
                (EventType.INVESTIGATION_CREATED, {}),
                (EventType.INVESTIGATION_STARTED, {"title": f"Investigation {i+1}"}),
            ]
            # Add varying number of alerts
            for j in range(i + 1):
                events.append((
                    EventType.ALERT_CORRELATED,
                    {"alert_id": f"a{j}", "severity": "medium"}
                ))

            for event_type, data in events:
                event = await store.append(
                    aggregate_id=agg_id,
                    event_type=event_type,
                    data=data,
                )
                await projector.project(event)

            expected_states[agg_id] = {"alert_count": i + 1}

        await clean_session.commit()

        # Delete all read models
        await clean_session.execute(delete(InvestigationReadModel))
        await clean_session.commit()

        # Recovery: get all aggregate IDs and replay
        all_ids = await store.get_all_aggregate_ids()
        assert len(all_ids) == 3

        for agg_id in all_ids:
            events = await store.get_events(agg_id)
            for event in events:
                await projector.project(event)

        await clean_session.commit()

        # Verify all aggregates recovered
        from sqlalchemy import select
        for agg_id, expected in expected_states.items():
            result = await clean_session.execute(
                select(InvestigationReadModel).where(
                    InvestigationReadModel.id == agg_id
                )
            )
            investigation = result.scalar_one()
            assert investigation.alert_count == expected["alert_count"]


@pytest.mark.integration
class TestProjectingEventStoreIntegration:
    """Integration tests for ProjectingEventStore."""

    async def test_projecting_store_auto_projects(self, clean_session):
        """Test that ProjectingEventStore automatically projects events."""
        pstore = ProjectingEventStore(clean_session)
        aggregate_id = uuid4()

        # Append events - they should be auto-projected
        await pstore.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_CREATED,
            data={},
        )
        await pstore.append(
            aggregate_id=aggregate_id,
            event_type=EventType.INVESTIGATION_STARTED,
            data={"title": "Auto-projected Investigation"},
        )
        await pstore.append(
            aggregate_id=aggregate_id,
            event_type=EventType.ALERT_CORRELATED,
            data={"alert_id": "a1", "severity": "high"},
        )
        await clean_session.commit()

        # Verify read model was created and updated
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        investigation = result.scalar_one()

        assert investigation.title == "Auto-projected Investigation"
        assert investigation.status == "in_progress"
        assert investigation.alert_count == 1
        assert investigation.max_severity == "high"

    async def test_projecting_store_batch_append(self, clean_session):
        """Test batch append with auto-projection."""
        pstore = ProjectingEventStore(clean_session)
        aggregate_id = uuid4()

        events = [
            (EventType.INVESTIGATION_CREATED, {}, None),
            (EventType.INVESTIGATION_STARTED, {"title": "Batch Test"}, None),
            (EventType.ALERT_CORRELATED, {"alert_id": "a1", "severity": "medium"}, None),
            (EventType.ALERT_CORRELATED, {"alert_id": "a2", "severity": "high"}, None),
            (EventType.OBSERVABLE_EXTRACTED, {"type": "ip", "value": "1.1.1.1"}, None),
        ]

        await pstore.append_batch(aggregate_id, events)
        await clean_session.commit()

        # Verify all events projected
        from sqlalchemy import select
        result = await clean_session.execute(
            select(InvestigationReadModel).where(
                InvestigationReadModel.id == aggregate_id
            )
        )
        investigation = result.scalar_one()

        assert investigation.title == "Batch Test"
        assert investigation.alert_count == 2
        assert investigation.observable_count == 1
        assert investigation.max_severity == "high"
