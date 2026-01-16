"""Persistence layer for soctalk - event sourcing with PostgreSQL."""

from soctalk.persistence.checkpointing import (
    create_checkpointer,
    get_checkpoint_config,
    get_checkpoint_connection_string,
    get_checkpointer,
    get_thread_id_for_investigation,
)
from soctalk.persistence.database import (
    close_db,
    get_async_engine,
    get_async_session,
    init_db,
)
from soctalk.persistence.emitter import (
    EventEmitter,
    get_emitter_from_state,
    get_investigation_id_from_state,
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
from soctalk.persistence.projector import ProjectingEventStore, Projector
from soctalk.persistence.store import ConcurrencyError, EventStore, IdempotencyError

__all__ = [
    # Checkpointing
    "create_checkpointer",
    "get_checkpoint_config",
    "get_checkpoint_connection_string",
    "get_checkpointer",
    "get_thread_id_for_investigation",
    # Database
    "close_db",
    "get_async_engine",
    "get_async_session",
    "init_db",
    # Event Emitter
    "EventEmitter",
    "get_emitter_from_state",
    "get_investigation_id_from_state",
    # Events
    "Event",
    "EventStore",
    "EventType",
    # Projector
    "Projector",
    "ProjectingEventStore",
    # Errors
    "ConcurrencyError",
    "IdempotencyError",
    # Read Models
    "AnalyzerStats",
    "InvestigationReadModel",
    "IOCStats",
    "MetricsHourly",
    "RuleStats",
]
