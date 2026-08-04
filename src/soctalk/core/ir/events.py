"""Event kinds, signatures, and idempotency helpers.

Events are the only write path for investigation state. The reducer is the only
reader that mutates the projection. Everything else queries the
projection or appends new events.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Event kinds (closed enum)
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    # Ingest
    ALERT_INGESTED = "alert_ingested"
    EXTERNAL_SIGNAL = "external_signal"

    # AI-side state updates
    HYPOTHESIS_UPDATED = "hypothesis_updated"
    IOC_ADDED = "ioc_added"
    IOC_REMOVED = "ioc_removed"
    ASSET_LINKED = "asset_linked"
    ASSET_UNLINKED = "asset_unlinked"
    TIMELINE_ENTRY = "timeline_entry"
    DIRECTIVE_ADDED = "directive_added"
    DIRECTIVE_REMOVED = "directive_removed"
    POLICY_BOUND = "policy_bound"
    STATUS_CHANGED = "status_changed"
    CONFIDENCE_RECALIBRATED = "confidence_recalibrated"
    AI_MESSAGE = "ai_message"

    # Tool interaction
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_RESULT = "tool_result"

    # Proposal lifecycle events (recorded in inbox for replay)
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_EXECUTED = "proposal_executed"
    PROPOSAL_FAILED = "proposal_failed"

    # Human-side inputs
    ANALYST_MESSAGE = "analyst_message"
    ANALYST_CORRECTION = "analyst_correction"
    ANALYST_COMMAND = "analyst_command"

    # Case lifecycle
    REOPENED = "reopened"
    AUTO_CLOSED = "auto_closed"
    BUDGET_WARNING = "budget_warning"
    BUDGET_HALT = "budget_halt"

    # Pipeline replay beats (issue #72). Emitted by the triage pipeline —
    # graph nodes via the worker event sink, plus the L1 close paths — and
    # consumed by the flight-recorder surfaces. Never applied by the
    # reducer: the projection is row-side state; these are the journey.
    POLICY_RESOLVED = "policy_resolved"
    SUPERVISOR_DECISION = "supervisor_decision"
    WORKER_STARTED = "worker_started"
    WORKER_RESULT = "worker_result"
    VERDICT_RENDERED = "verdict_rendered"
    GUARD_EVALUATED = "guard_evaluated"
    HUMAN_REVIEW_REQUESTED = "human_review_requested"
    HUMAN_DECISION = "human_decision"


# Events that ONLY the reducer applies to the projection.
REDUCER_APPLIES: frozenset[EventKind] = frozenset(
    {
        EventKind.ALERT_INGESTED,
        EventKind.HYPOTHESIS_UPDATED,
        EventKind.IOC_ADDED,
        EventKind.IOC_REMOVED,
        EventKind.ASSET_LINKED,
        EventKind.ASSET_UNLINKED,
        EventKind.TIMELINE_ENTRY,
        EventKind.DIRECTIVE_ADDED,
        EventKind.DIRECTIVE_REMOVED,
        EventKind.POLICY_BOUND,
        EventKind.STATUS_CHANGED,
        EventKind.CONFIDENCE_RECALIBRATED,
        EventKind.ANALYST_CORRECTION,
        EventKind.REOPENED,
        EventKind.AUTO_CLOSED,
    }
)


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# Signatures (for coalescing)
# ---------------------------------------------------------------------------


def alert_signature(rule_id: str | None, asset_ids: list[str], ts: datetime) -> str:
    """Coalescing signature for alert_ingested events.

    Same rule firing on the same asset within a 5-minute bucket merges
    into a single alert row rather than spawning N cases.
    """

    bucket = int(ts.timestamp() // 300)  # 5-minute buckets
    asset_key = ",".join(sorted(asset_ids))
    basis = f"{rule_id or ''}|{asset_key}|{bucket}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def ioc_fingerprint(ioc_type: str, value: str) -> str:
    """Stable fingerprint for an IOC across cases."""

    return hashlib.sha256(f"{ioc_type}|{value}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def event_idempotency_key(
    investigation_id: UUID,
    kind: EventKind,
    payload: dict[str, Any],
    producer: str | None = None,
) -> str:
    """Deterministic idempotency key for an event.

    Duplicate inserts with the same key silently return the existing
    row. Producer is included so two different sources emitting
    identical payloads both land (unlikely but safe).
    """

    basis = canonical_json(
        {
            "investigation_id": str(investigation_id),
            "kind": kind.value,
            "payload": payload,
            "producer": producer or "app",
        }
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def worker_event_idempotency_key(
    *, run_id: UUID, lease_id: UUID, client_ord: int
) -> str:
    """Per-lease idempotency for worker-emitted replay beats (issue #72).

    A retried batch dedupes; a reclaimed lease's re-emission never
    collides with the dead lease's rows."""

    basis = canonical_json(
        {
            "kind": "worker-replay",
            "run_id": str(run_id),
            "lease_id": str(lease_id),
            "client_ord": client_ord,
        }
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# Beats that must appear AT MOST ONCE per run, no matter which lease emits
# them. A reclaimed run rebuilds graph state from the claim and re-runs the
# graph, so it can legitimately re-cross the same threshold — but the record
# is a per-run fact, not a per-lease one. These dedupe on (run_id, kind), so
# the second crossing collapses onto the first row instead of the per-lease
# key minting a duplicate (#103 soft warning).
RUN_SCOPED_WORKER_EVENTS: frozenset[EventKind] = frozenset({EventKind.BUDGET_WARNING})


def run_scoped_event_idempotency_key(*, run_id: UUID, kind: EventKind) -> str:
    """At-most-once-per-run idempotency for a lease-independent beat."""

    basis = canonical_json(
        {
            "kind": "worker-replay-run",
            "run_id": str(run_id),
            "event_kind": kind.value,
        }
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def proposal_idempotency_key(
    investigation_id: UUID, action_type: str, params: dict[str, Any]
) -> str:
    basis = canonical_json(
        {
            "investigation_id": str(investigation_id),
            "action_type": action_type,
            "params": params,
        }
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Event append (idempotent)
# ---------------------------------------------------------------------------


async def append_event(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    run_id: UUID | None,
    kind: EventKind,
    payload: dict[str, Any],
    visibility: str = "mssp_only",
    causation_event_id: UUID | None = None,
    correlation_id: UUID | None = None,
    idempotency_key: str | None = None,
    producer: str | None = None,
) -> UUID:
    """Append an event to the investigation inbox. Returns the event_id.

    Idempotent: re-inserting the same (investigation_id, idempotency_key) returns
    the original event_id. Uses ON CONFLICT DO NOTHING RETURNING so a
    duplicate never raises IntegrityError — the prior pattern rolled
    back the whole session, silently discarding any writes a caller had
    made in the same transaction before the append.
    """

    key = idempotency_key or event_idempotency_key(
        investigation_id, kind, payload, producer=producer
    )
    event_id = uuid4()
    result = await db.execute(
        text(
            """
            INSERT INTO investigation_events
              (event_id, tenant_id, investigation_id, run_id, kind, payload,
               causation_event_id, correlation_id, idempotency_key,
               visibility)
            VALUES
              (:event_id, :tenant_id, :investigation_id, :run_id, :kind,
               CAST(:payload AS JSONB),
               :causation_event_id, :correlation_id, :idempotency_key,
               :visibility)
            ON CONFLICT (investigation_id, idempotency_key) DO NOTHING
            RETURNING event_id
            """
        ),
        {
            "event_id": str(event_id),
            "tenant_id": str(tenant_id),
            "investigation_id": str(investigation_id),
            "run_id": str(run_id) if run_id else None,
            "kind": kind.value,
            "payload": canonical_json(payload),
            "causation_event_id": str(causation_event_id)
            if causation_event_id
            else None,
            "correlation_id": str(correlation_id) if correlation_id else None,
            "idempotency_key": key,
            "visibility": visibility,
        },
    )
    inserted = result.scalar_one_or_none()
    if inserted is not None:
        return UUID(str(inserted))

    # Duplicate — look up existing event. No rollback needed; the
    # INSERT was a no-op at the DB level.
    row = (
        await db.execute(
            text(
                "SELECT event_id FROM investigation_events "
                "WHERE investigation_id = :investigation_id AND idempotency_key = :key"
            ),
            {"investigation_id": str(investigation_id), "key": key},
        )
    ).scalar_one()
    return UUID(str(row))


__all__ = [
    "EventKind",
    "REDUCER_APPLIES",
    "RUN_SCOPED_WORKER_EVENTS",
    "alert_signature",
    "append_event",
    "canonical_json",
    "event_idempotency_key",
    "ioc_fingerprint",
    "proposal_idempotency_key",
    "run_scoped_event_idempotency_key",
    "worker_event_idempotency_key",
]
