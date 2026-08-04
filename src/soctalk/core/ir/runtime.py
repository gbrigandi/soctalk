"""Case run engine: inbox consumer, proposals, outbox executor.

No LangGraph in MVP — plain async functions. If an investigation needs more
sophisticated planning later, layer it under the same interfaces.

Authoritative state machine: core-invariants §4 (runs), §6 (proposals).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.ir.events import (
    EventKind,
    append_event,
    canonical_json,
    proposal_idempotency_key,
)
from soctalk.core.ir.policies import resolve_run_token_budget
from soctalk.core.ir.reducer import apply_event, load_facts, save_facts
from soctalk.core.observability.audit import log_audit

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


async def start_run(
    db: AsyncSession,
    tenant_id: UUID,
    investigation_id: UUID,
    settle_seconds: float = 0.0,
) -> UUID:
    """Create an active run for an investigation. Fails if one already exists.

    ``settle_seconds`` delays claimability (issue #28 settle window) so a
    burst of correlated events accumulates onto the investigation before the
    first LLM look. Pass 0 (default) for immediate claim — used for the
    high-severity bypass and for reopens.
    """

    run_id = uuid4()
    # Resolve the per-run token budget now and stamp it on the row (#103):
    # install default -> tenant override, clamped to the install cap. Resolving
    # at creation makes the value immutable for this run, so a later override
    # change never touches an in-flight run, and it takes effect with no worker
    # rollout. Previously the INSERT omitted tokens_budget and every run rode
    # the column default.
    tokens_budget = await resolve_run_token_budget(db, tenant_id)
    # X in "re-triage up to X attempts". The column's DB default is 4
    # (migration v1_0039); the env lets an operator raise or lower it for new
    # runs without a migration. Read per call so a config change applies to
    # the next run rather than the next process. Values below 1 are ignored
    # rather than honoured: a run that may never be attempted is not a run.
    max_attempts = 0
    raw = os.environ.get("SOCTALK_MAX_TRIAGE_ATTEMPTS", "")
    if raw.strip():
        try:
            max_attempts = int(raw)
        except ValueError:
            logger.warning("bad_max_triage_attempts value=%r -> DB default", raw)
    if max_attempts >= 1:
        await db.execute(
            text(
                "INSERT INTO investigation_runs "
                "  (id, tenant_id, investigation_id, status, not_before, "
                "   max_attempts, tokens_budget) "
                "VALUES (:id, :t, :c, 'active', "
                "        now() + make_interval(secs => :settle), :cap, :budget)"
            ),
            {
                "id": str(run_id),
                "t": str(tenant_id),
                "c": str(investigation_id),
                "settle": max(0.0, float(settle_seconds)),
                "cap": max_attempts,
                "budget": tokens_budget,
            },
        )
    else:
        await db.execute(
            text(
                "INSERT INTO investigation_runs "
                "  (id, tenant_id, investigation_id, status, not_before, tokens_budget) "
                "VALUES (:id, :t, :c, 'active', "
                "        now() + make_interval(secs => :settle), :budget)"
            ),
            {
                "id": str(run_id),
                "t": str(tenant_id),
                "c": str(investigation_id),
                "settle": max(0.0, float(settle_seconds)),
                "budget": tokens_budget,
            },
        )
    return run_id


async def transition_run(
    db: AsyncSession, run_id: UUID, new_status: str, *, last_error: str | None = None
) -> None:
    """Update run status. Does not validate transitions here; callers are
    responsible for not doing illegal transitions. The unique partial
    index enforces at-most-one-active at the DB level."""

    await db.execute(
        text(
            "UPDATE investigation_runs SET status = :s, "
            "       ended_at = CASE WHEN :s IN ('completed','failed') THEN now() ELSE ended_at END, "
            "       last_error = :e "
            "WHERE id = :id"
        ),
        {"s": new_status, "id": str(run_id), "e": last_error},
    )


async def active_run_for_case(
    db: AsyncSession, investigation_id: UUID
) -> UUID | None:
    row = (
        await db.execute(
            text(
                "SELECT id FROM investigation_runs "
                "WHERE investigation_id = :c "
                "AND status IN ('active','paused','waiting_on_gate','halted_budget') "
                "LIMIT 1"
            ),
            {"c": str(investigation_id)},
        )
    ).scalar_one_or_none()
    return UUID(str(row)) if row else None


# ---------------------------------------------------------------------------
# Reducer-driven inbox consumption
# ---------------------------------------------------------------------------


async def consume_new_events(
    db: AsyncSession, tenant_id: UUID, investigation_id: UUID
) -> int:
    """Apply any events since applied_seq and persist the projection.

    Returns the number of events consumed.
    """

    facts = await load_facts(db, investigation_id)
    rows = (
        await db.execute(
            text(
                "SELECT seq, kind, payload FROM investigation_events "
                "WHERE investigation_id = :c AND seq > :s ORDER BY seq ASC"
            ),
            {"c": str(investigation_id), "s": facts.applied_seq},
        )
    ).mappings().all()
    if not rows:
        return 0

    for row in rows:
        facts = apply_event(facts, row["kind"], dict(row["payload"]), row["seq"])
    await save_facts(db, tenant_id, investigation_id, facts)
    return len(rows)


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


async def create_proposal(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    investigation_id: UUID,
    run_id: UUID | None,
    action_type: str,
    params: dict[str, Any],
    rationale: str,
    capability_class: str,
    blast_radius: str | None = None,
) -> UUID:
    """Create a proposal + emit proposal_created event + flip run to gate.

    Idempotent on (investigation_id, action_type, canonical(params)).
    """

    key = proposal_idempotency_key(investigation_id, action_type, params)
    proposal_id = uuid4()

    # INSERT with ON CONFLICT DO NOTHING to enforce idempotency.
    result = await db.execute(
        text(
            """
            INSERT INTO proposals
              (id, tenant_id, investigation_id, run_id, action_type, params,
               rationale, blast_radius, capability_class, status,
               idempotency_key, visibility)
            VALUES
              (:id, :t, :c, :r, :at, CAST(:p AS JSONB),
               :rat, :br, :cc, 'proposed',
               :ik, 'mssp_only')
            ON CONFLICT (investigation_id, idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": str(proposal_id),
            "t": str(tenant_id),
            "c": str(investigation_id),
            "r": str(run_id) if run_id else None,
            "at": action_type,
            "p": canonical_json(params),
            "rat": rationale,
            "br": blast_radius,
            "cc": capability_class,
            "ik": key,
        },
    )
    row = result.scalar_one_or_none()
    if row is None:
        # Duplicate — return existing.
        existing = (
            await db.execute(
                text(
                    "SELECT id FROM proposals "
                    "WHERE investigation_id = :c AND idempotency_key = :k"
                ),
                {"c": str(investigation_id), "k": key},
            )
        ).scalar_one()
        return UUID(str(existing))

    # Log the proposal creation.
    await append_event(
        db,
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        run_id=run_id,
        kind=EventKind.PROPOSAL_CREATED,
        payload={
            "proposal_id": str(proposal_id),
            "action_type": action_type,
            "capability_class": capability_class,
        },
        producer="ai",
    )
    if run_id is not None:
        await transition_run(db, run_id, "waiting_on_gate")

    await log_audit(
        db,
        action="ir.proposal.created",
        actor_principal="ai",
        actor_id=f"run:{run_id}" if run_id else "ai",
        tenant_id=tenant_id,
        resource_type="proposal",
        resource_id=str(proposal_id),
    )
    return proposal_id


async def approve_proposal(
    db: AsyncSession,
    *,
    proposal_id: UUID,
    approver_user_id: UUID,
    reason: str,
) -> None:
    """Approve a proposal. Enqueues execute_proposal on the outbox."""

    row = (
        await db.execute(
            text(
                "SELECT tenant_id, investigation_id, run_id, action_type, params, "
                "       capability_class, idempotency_key, status "
                "FROM proposals WHERE id = :id"
            ),
            {"id": str(proposal_id)},
        )
    ).mappings().first()
    if row is None:
        raise ValueError(f"proposal {proposal_id} not found")
    if row["status"] != "proposed":
        raise ValueError(f"proposal {proposal_id} is in status {row['status']}, cannot approve")

    await db.execute(
        text(
            "UPDATE proposals SET status = 'approved', approver_user_id = :u, "
            "       approval_reason = :r, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": str(proposal_id), "u": str(approver_user_id), "r": reason},
    )

    # Enqueue outbox row for the executor.
    outbox_key = f"proposal:{proposal_id}"
    await db.execute(
        text(
            """
            INSERT INTO investigation_outbox
              (id, tenant_id, investigation_id, kind, idempotency_key, payload, status)
            VALUES
              (:id, :t, :c, 'execute_proposal', :ik, CAST(:p AS JSONB), 'pending')
            ON CONFLICT (idempotency_key) DO NOTHING
            """
        ),
        {
            "id": str(uuid4()),
            "t": str(row["tenant_id"]),
            "c": str(row["investigation_id"]),
            "ik": outbox_key,
            "p": canonical_json(
                {
                    "proposal_id": str(proposal_id),
                    "action_type": row["action_type"],
                    "params": row["params"],
                    "capability_class": row["capability_class"],
                }
            ),
        },
    )

    # Append event so reducer and run can see it.
    await append_event(
        db,
        tenant_id=UUID(str(row["tenant_id"])),
        investigation_id=UUID(str(row["investigation_id"])),
        run_id=UUID(str(row["run_id"])) if row["run_id"] else None,
        kind=EventKind.PROPOSAL_APPROVED,
        payload={"proposal_id": str(proposal_id), "reason": reason},
        producer="human",
    )

    # Resume run.
    if row["run_id"]:
        await transition_run(db, UUID(str(row["run_id"])), "active")

    await log_audit(
        db,
        action="ir.proposal.approved",
        actor_principal="user",
        actor_id=str(approver_user_id),
        tenant_id=UUID(str(row["tenant_id"])),
        resource_type="proposal",
        resource_id=str(proposal_id),
        notes=reason,
    )


async def reject_proposal(
    db: AsyncSession,
    *,
    proposal_id: UUID,
    approver_user_id: UUID,
    reason: str,
) -> None:
    row = (
        await db.execute(
            text(
                "SELECT tenant_id, investigation_id, run_id, status "
                "FROM proposals WHERE id = :id"
            ),
            {"id": str(proposal_id)},
        )
    ).mappings().first()
    if row is None:
        raise ValueError(f"proposal {proposal_id} not found")
    if row["status"] != "proposed":
        raise ValueError(
            f"proposal {proposal_id} is in status {row['status']}, cannot reject"
        )

    await db.execute(
        text(
            "UPDATE proposals SET status = 'rejected', approver_user_id = :u, "
            "       rejected_reason = :r, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": str(proposal_id), "u": str(approver_user_id), "r": reason},
    )

    await append_event(
        db,
        tenant_id=UUID(str(row["tenant_id"])),
        investigation_id=UUID(str(row["investigation_id"])),
        run_id=UUID(str(row["run_id"])) if row["run_id"] else None,
        kind=EventKind.PROPOSAL_REJECTED,
        payload={"proposal_id": str(proposal_id), "reason": reason},
        producer="human",
    )

    if row["run_id"]:
        await transition_run(db, UUID(str(row["run_id"])), "active")

    await log_audit(
        db,
        action="ir.proposal.rejected",
        actor_principal="user",
        actor_id=str(approver_user_id),
        tenant_id=UUID(str(row["tenant_id"])),
        resource_type="proposal",
        resource_id=str(proposal_id),
        notes=reason,
    )


# ---------------------------------------------------------------------------
# Outbox executor
# ---------------------------------------------------------------------------


LEASE_SECONDS = 60
MAX_BACKOFF_SECONDS = 30 * 60


def _backoff(attempts: int) -> int:
    return min(10 * (2 ** attempts), MAX_BACKOFF_SECONDS)


async def claim_next_outbox(
    db: AsyncSession, worker_id: str, kinds: tuple[str, ...] | None = None
) -> dict[str, Any] | None:
    """Claim the next pending outbox row with a 60s lease.

    Uses FOR UPDATE SKIP LOCKED to avoid workers fighting over rows.
    ``kinds`` scopes the claim: an executor that only knows how to handle
    certain kinds must not claim (and terminally fail) everyone else's rows.
    """

    row = (
        await db.execute(
            text(
                """
                SELECT id FROM investigation_outbox
                WHERE (status = 'pending' AND next_attempt_at <= now()
                   OR (status = 'in_flight'
                       AND claimed_at < now() - make_interval(secs => :lease)))
                  AND (:kinds_all OR kind = ANY(:kinds))
                ORDER BY next_attempt_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ),
            {
                "lease": LEASE_SECONDS,
                "kinds_all": kinds is None,
                "kinds": list(kinds or ()),
            },
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    await db.execute(
        text(
            "UPDATE investigation_outbox SET status = 'in_flight', claimed_at = now(), "
            "       claimed_by = :w, updated_at = now() WHERE id = :id"
        ),
        {"id": str(row), "w": worker_id},
    )

    full = (
        await db.execute(
            text("SELECT * FROM investigation_outbox WHERE id = :id"),
            {"id": str(row)},
        )
    ).mappings().first()
    return dict(full) if full else None


async def mark_outbox_succeeded(
    db: AsyncSession, outbox_id: UUID, external_ref: str | None = None
) -> None:
    await db.execute(
        text(
            "UPDATE investigation_outbox SET status = 'succeeded', succeeded_at = now(), "
            "       external_ref = COALESCE(:xref, external_ref), updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": str(outbox_id), "xref": external_ref},
    )


async def mark_outbox_failed(
    db: AsyncSession,
    outbox_id: UUID,
    err: str,
) -> None:
    """Record failure; reschedule if under max_attempts, else terminal."""

    row = (
        await db.execute(
            text(
                "SELECT attempts, max_attempts FROM investigation_outbox WHERE id = :id"
            ),
            {"id": str(outbox_id)},
        )
    ).mappings().first()
    if row is None:
        return
    attempts = row["attempts"] + 1
    terminal = attempts >= row["max_attempts"]
    if terminal:
        await db.execute(
            text(
                "UPDATE investigation_outbox SET status = 'failed', attempts = :a, "
                "       last_error = :e, updated_at = now() WHERE id = :id"
            ),
            {"id": str(outbox_id), "a": attempts, "e": err},
        )
    else:
        delay = _backoff(attempts)
        await db.execute(
            text(
                "UPDATE investigation_outbox SET status = 'pending', attempts = :a, "
                "       last_error = :e, claimed_at = NULL, claimed_by = NULL, "
                "       next_attempt_at = now() + make_interval(secs => :d), "
                "       updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": str(outbox_id), "a": attempts, "e": err, "d": delay},
        )


async def execute_one(
    db: AsyncSession,
    worker_id: str,
    handlers: dict[str, Any] | None = None,
    *,
    kinds: tuple[str, ...] | None = None,
) -> bool:
    """Claim and execute a single outbox row. Returns True if work was done.

    ``handlers`` maps ``kind`` → async callable(db, outbox_row) → Optional[external_ref].
    Unknown kinds are marked failed with a clear error. Defaults to
    :func:`default_handlers` if none provided. ``kinds`` restricts which rows
    this executor claims at all (see :func:`claim_next_outbox`).
    """

    handlers = handlers or default_handlers()
    row = await claim_next_outbox(db, worker_id, kinds)
    if row is None:
        return False

    kind = row["kind"]
    handler = handlers.get(kind)
    try:
        if handler is None:
            raise ValueError(f"no handler registered for outbox kind {kind!r}")
        external_ref = await handler(db, row)
        await mark_outbox_succeeded(
            db, UUID(str(row["id"])), external_ref=external_ref
        )
        await log_audit(
            db,
            action=f"ir.outbox.{kind}.succeeded",
            actor_principal="executor",
            actor_id=worker_id,
            tenant_id=UUID(str(row["tenant_id"])),
            resource_type="outbox",
            resource_id=str(row["id"]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("outbox_execution_failed", outbox_id=str(row["id"]))
        await mark_outbox_failed(db, UUID(str(row["id"])), str(exc))
        await log_audit(
            db,
            action=f"ir.outbox.{kind}.failed",
            actor_principal="executor",
            actor_id=worker_id,
            tenant_id=UUID(str(row["tenant_id"])),
            resource_type="outbox",
            resource_id=str(row["id"]),
            notes=str(exc)[:500],
        )
    return True


# ---------------------------------------------------------------------------
# Built-in executor handlers
# ---------------------------------------------------------------------------


async def _handle_execute_proposal(
    db: AsyncSession, outbox_row: dict[str, Any]
) -> str | None:
    """Execute an approved proposal.

    Advances proposal → executing → executed (or failed) and emits
    proposal_executed / proposal_failed events into the investigation inbox so
    the reducer and UI see the completion.

    Tool invocation is a stub in MVP: the tool registry returns
    ``{"not_implemented": true}`` for built-in tools. Proposals complete
    successfully with that marker recorded on the outbox row. Real tool
    wiring lands with the tool-specific integrations.
    """

    from soctalk.core.ir.events import EventKind, append_event
    from soctalk.core.tenancy.context import tenant_context

    payload = dict(outbox_row.get("payload") or {})
    proposal_id = UUID(payload["proposal_id"])
    action_type = payload.get("action_type", "")

    # Resolve the proposal's full context.
    row = (
        await db.execute(
            text(
                "SELECT tenant_id, investigation_id, run_id, params, capability_class "
                "FROM proposals WHERE id = :id"
            ),
            {"id": str(proposal_id)},
        )
    ).mappings().first()
    if row is None:
        raise RuntimeError(f"proposal {proposal_id} vanished before execution")

    tenant_id = UUID(str(row["tenant_id"]))
    investigation_id = UUID(str(row["investigation_id"]))
    run_id = UUID(str(row["run_id"])) if row["run_id"] else None

    # Mark as executing before the tool runs so the UI reflects the
    # transition. Writes need tenant_context so RLS WITH CHECK passes.
    async with tenant_context(db, tenant_id):
        await db.execute(
            text(
                "UPDATE proposals SET status = 'executing', updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": str(proposal_id)},
        )

        try:
            result = await _invoke_tool(action_type, dict(row["params"]))
            # On success: record the result on the outbox payload + event.
            await db.execute(
                text(
                    "UPDATE proposals SET status = 'executed', updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": str(proposal_id)},
            )
            await append_event(
                db,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                run_id=run_id,
                kind=EventKind.PROPOSAL_EXECUTED,
                payload={
                    "proposal_id": str(proposal_id),
                    "action_type": action_type,
                    "result": result,
                },
                producer="executor",
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Failure path: mark proposal failed, emit proposal_failed,
            # then re-raise so the outbox row itself is retried per its
            # max_attempts budget.
            await db.execute(
                text(
                    "UPDATE proposals SET status = 'failed', updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": str(proposal_id)},
            )
            await append_event(
                db,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                run_id=run_id,
                kind=EventKind.PROPOSAL_FAILED,
                payload={
                    "proposal_id": str(proposal_id),
                    "action_type": action_type,
                    "error": str(exc)[:500],
                },
                producer="executor",
            )
            raise


async def _invoke_tool(action_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an action to the tool registry.

    In MVP the built-in tools are stubs that return
    ``{"not_implemented": true, ...}``. Unknown actions raise.
    """

    from soctalk.core.ir.tools import registry

    spec = registry.get(action_type)
    if spec is None or spec.handler is None:
        raise RuntimeError(f"no tool handler for action {action_type!r}")
    return await spec.handler(**params)


def default_handlers() -> dict[str, Any]:
    """Return the default outbox kind → handler map."""

    return {
        "execute_proposal": _handle_execute_proposal,
    }


__all__ = [
    "active_run_for_case",
    "approve_proposal",
    "claim_next_outbox",
    "consume_new_events",
    "create_proposal",
    "default_handlers",
    "execute_one",
    "mark_outbox_failed",
    "mark_outbox_succeeded",
    "reject_proposal",
    "start_run",
    "transition_run",
]
