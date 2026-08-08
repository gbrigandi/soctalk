"""Pytest fixtures for soctalk tests.

Module-level side effect: default ``DATABASE_URL_ADMIN/APP/MSSP`` to the
local integration Postgres on port 5432 if the caller didn't set them.
Matches the URL shape that ``just integration-up`` provisions and that CI
exports in ``.github/workflows/v1-ci.yml``.

The V1 RLS / IR test files read these vars via their own ``_url()`` helpers.
Two provisioning test files (``test_provisioning_controller.py``,
``test_provisioning_k3d_live.py``) carry a stale port-5444 default in
their local helpers; setting the env vars here overrides those stale
defaults so ``patagon_check`` and other bare-``pytest`` invocations talk
to the same Postgres the rest of the suite uses.

Local override: a gitignored ``.env.test`` at the repo root lets a dev
box whose port 5432 is occupied by an unrelated Postgres repoint
``DATABASE_URL[_ADMIN/_APP/_MSSP]`` at wherever its integration Postgres
actually listens, without exporting vars into every pytest invocation.
Loaded with ``override=True``: the file is purpose-built for the test
process, so it beats stale URLs inherited from the dev shell (e.g. a
flake/direnv-exported ``DATABASE_URL``). CI is unaffected — it never
ships a ``.env.test``.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from dotenv import load_dotenv

    # override=True: .env.test (when present) beats inherited shell vars —
    # see the module docstring for why.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.test", override=True)
except ImportError:  # pragma: no cover - python-dotenv is a core dependency
    pass

os.environ.setdefault(
    "DATABASE_URL_ADMIN",
    "postgresql+asyncpg://soctalk_admin:soctalk_admin@localhost:5432/soctalk",
)
os.environ.setdefault(
    "DATABASE_URL_APP",
    "postgresql+asyncpg://soctalk_app:soctalk_app@localhost:5432/soctalk",
)
os.environ.setdefault(
    "DATABASE_URL_MSSP",
    "postgresql+asyncpg://soctalk_mssp:soctalk_mssp@localhost:5432/soctalk",
)

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