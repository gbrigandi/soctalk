"""Pytest fixtures for soctalk tests."""

from datetime import datetime
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.persistence.events import EventType
from soctalk.persistence.models import (
    AnalyzerStats,
    Event,
    InvestigationReadModel,
    IOCStats,
    MetricsHourly,
    RuleStats,
)


@pytest.fixture
def mock_session() -> AsyncMock:
    """Create a mock async database session."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture
def sample_aggregate_id() -> UUID:
    """Create a sample aggregate ID for tests."""
    return uuid4()


@pytest.fixture
def sample_event(sample_aggregate_id: UUID) -> Event:
    """Create a sample event for tests."""
    return Event(
        id=uuid4(),
        aggregate_id=sample_aggregate_id,
        aggregate_type="Investigation",
        event_type=EventType.INVESTIGATION_CREATED.value,
        version=1,
        timestamp=datetime.utcnow(),
        data={},
        event_metadata={},
    )


@pytest.fixture
def sample_investigation(sample_aggregate_id: UUID) -> InvestigationReadModel:
    """Create a sample investigation read model for tests."""
    return InvestigationReadModel(
        id=sample_aggregate_id,
        status="pending",
        phase="triage",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


@pytest.fixture
def sample_hourly_metrics() -> MetricsHourly:
    """Create sample hourly metrics for tests."""
    return MetricsHourly(
        hour=datetime.utcnow().replace(minute=0, second=0, microsecond=0),
        investigations_created=0,
        investigations_closed=0,
    )


@pytest.fixture
def sample_ioc_stats() -> IOCStats:
    """Create sample IOC stats for tests."""
    return IOCStats(
        id=uuid4(),
        value="192.168.1.1",
        type="ip",
        times_seen=1,
    )


@pytest.fixture
def sample_rule_stats() -> RuleStats:
    """Create sample rule stats for tests."""
    return RuleStats(
        rule_id="100001",
        times_triggered=0,
    )


@pytest.fixture
def sample_analyzer_stats() -> AnalyzerStats:
    """Create sample analyzer stats for tests."""
    return AnalyzerStats(
        analyzer="VirusTotal",
        invocations=0,
        successes=0,
        failures=0,
    )


def create_event(
    aggregate_id: UUID,
    event_type: EventType,
    version: int = 1,
    data: dict | None = None,
    metadata: dict | None = None,
) -> Event:
    """Helper function to create events for tests."""
    return Event(
        id=uuid4(),
        aggregate_id=aggregate_id,
        aggregate_type="Investigation",
        event_type=event_type.value,
        version=version,
        timestamp=datetime.utcnow(),
        data=data or {},
        event_metadata=metadata or {},
    )
