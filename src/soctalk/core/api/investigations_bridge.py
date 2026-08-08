"""Bridge endpoints for the canonical ``frontend/`` UI.

The legacy SocTalk dashboard expected ``/api/investigations`` against the
single-tenant ``Investigation`` event-sourcing tables. The V1 multi-tenant
install replaces those with ``cases`` + ``investigation_runs``. This module maps
V1 records into the legacy ``InvestigationSummary``/``Investigation``
shape so ``frontend/`` works against the V1 backend without a rewrite.

Tenant scoping flows from the session:

  * ``mssp_admin`` / ``mssp_analyst`` see all tenants (audience='mssp',
    no ``app.current_tenant_id`` set).
  * ``tenant_*`` roles are pinned to their session tenant via the
    request middleware's ``set_request_db_context``; RLS enforces.
  * ``customer_viewer`` sees only customer-safe content (no verdict
    reasoning, no token spend) — gated UI-side via ``isCustomerScope``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.ir.labels import TERMINAL_STATUSES, cancel_investigation
from soctalk.core.tenancy.auth import current_identity
from soctalk.core.tenancy.context import tenant_context
from soctalk.core.tenancy.decorators import require_role
from soctalk.core.tenancy.models import Role

router = APIRouter(prefix="/api/investigations", tags=["investigations-bridge"])


class InvestigationSummary(BaseModel):
    id: str
    title: str | None
    status: str
    phase: str
    created_at: str
    updated_at: str
    closed_at: str | None
    alert_count: int
    observable_count: int
    malicious_count: int
    suspicious_count: int
    clean_count: int
    max_severity: str | None
    verdict_decision: str | None
    thehive_case_id: str | None
    # Tenant attribution for the cross-tenant MSSP view: which customer
    # this investigation belongs to. Always populated from RLS-visible
    # rows; the UI hides the column when the session is pinned to a
    # single tenant.
    tenant_id: str | None = None
    tenant_slug: str | None = None
    tenant_display_name: str | None = None


class Investigation(InvestigationSummary):
    time_to_triage_seconds: float | None
    time_to_verdict_seconds: float | None
    verdict_confidence: float | None
    verdict_reasoning: str | None
    threat_actor: str | None
    tags: list[str]
    tokens_used: int | None
    tokens_budget: int | None
    disposition: str | None
    # Enough to act on a budget halt from the investigation view (#127): which
    # run to unlock, and what it spent against its ceiling. MSSP-only, like the
    # token figures above — a customer sees neither cost nor run internals.
    run_id: str | None = None
    dollars_used: float | None = None
    dollars_budget: float | None = None


class InvestigationList(BaseModel):
    items: list[InvestigationSummary]
    total: int
    page: int
    page_size: int
    # The UI's Next button keys off this. It was never returned, so it read as
    # undefined and Next stayed disabled however many pages existed
    # (Codex review round 3, finding 1).
    has_more: bool = False


def _db(request: Request) -> AsyncSession:
    s = getattr(request.state, "db", None)
    if s is None:
        raise HTTPException(500, "db session not attached")
    return s


def _phase_from_status(status: str) -> str:
    # Map IR investigation status onto the legacy ``phase`` field. The legacy UI
    # uses phase as a coarse pipeline marker (triage / analysis /
    # verdict / closed); IR has a richer status set we collapse.
    if status in ("active",):
        return "analysis"
    if status in TERMINAL_STATUSES:
        return "closed"
    return status


def _wazuh_severity_label(level: int | None) -> str | None:
    if level is None:
        return None
    if level >= 12:
        return "critical"
    if level >= 8:
        return "high"
    if level >= 5:
        return "medium"
    return "low"


def _disposition(case_status: str, run_status: str | None) -> str | None:
    if case_status in ("auto_closed_fp", "closed_fp"):
        return "close_fp"
    if case_status in ("closed_tp", "escalated"):
        return "escalate"
    if run_status == "halted_budget":
        return "halted_budget"
    if run_status == "failed":
        return "failed"
    return None


@router.get("", response_model=InvestigationList)
async def list_investigations(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status: str | None = None,
    phase: str | None = None,
    severity: str | None = None,
) -> InvestigationList:
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "authentication required")

    db = _db(request)
    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if status:
        where_clauses.append("c.status = :status")
        params["status"] = status
    if severity:
        sev_map = {"low": (0, 4), "medium": (5, 7), "high": (8, 11), "critical": (12, 15)}
        if severity in sev_map:
            lo, hi = sev_map[severity]
            where_clauses.append("c.severity BETWEEN :sev_lo AND :sev_hi")
            params["sev_lo"] = lo
            params["sev_hi"] = hi
    if phase:
        if phase == "closed":
            where_clauses.append(
                "c.status IN ('auto_closed_fp','closed_fp','closed','closed_tp')"
            )
        elif phase == "analysis":
            where_clauses.append("c.status = 'active'")
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    rows = (
        await db.execute(
            text(
                f"""
                SELECT c.id, c.short_id, c.title, c.status, c.severity,
                       c.opened_at, c.updated_at, c.closed_at, c.summary,
                       c.close_reason,
                       c.tenant_id,
                       t.slug AS tenant_slug,
                       t.display_name AS tenant_display_name,
                       (
                         SELECT count(*) FROM alerts a WHERE a.investigation_id = c.id
                       ) AS alert_count,
                       (
                         SELECT coalesce(sum(jsonb_array_length(a.initial_iocs)), 0)
                         FROM alerts a WHERE a.investigation_id = c.id
                       ) AS observable_count,
                       (
                         SELECT cr.tokens_used FROM investigation_runs cr
                         WHERE cr.investigation_id = c.id
                         ORDER BY cr.started_at DESC LIMIT 1
                       ) AS tokens_used,
                       (
                         SELECT cr.status FROM investigation_runs cr
                         WHERE cr.investigation_id = c.id
                         ORDER BY cr.started_at DESC LIMIT 1
                       ) AS run_status
                FROM investigations c
                LEFT JOIN tenants t ON t.id = c.tenant_id
                {where_sql}
                ORDER BY c.opened_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
    ).mappings().all()

    total = (
        await db.execute(text(f"SELECT count(*) FROM investigations c {where_sql}"), params)
    ).scalar_one()

    items = [
        InvestigationSummary(
            id=str(r["id"]),
            title=r["title"] or r["short_id"],
            status=r["status"],
            phase=_phase_from_status(r["status"]),
            created_at=r["opened_at"].isoformat(),
            updated_at=r["updated_at"].isoformat(),
            closed_at=r["closed_at"].isoformat() if r["closed_at"] else None,
            alert_count=int(r["alert_count"] or 0),
            observable_count=int(r["observable_count"] or 0),
            malicious_count=0,
            suspicious_count=0,
            clean_count=0,
            max_severity=_wazuh_severity_label(int(r["severity"] or 0)),
            verdict_decision=_disposition(r["status"], r["run_status"]),
            thehive_case_id=None,
            tenant_id=str(r["tenant_id"]) if r["tenant_id"] else None,
            tenant_slug=r["tenant_slug"],
            tenant_display_name=r["tenant_display_name"],
        )
        for r in rows
    ]
    return InvestigationList(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < int(total),
    )


@router.get("/{investigation_id}", response_model=Investigation)
async def get_investigation(investigation_id: UUID, request: Request) -> Investigation:
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "authentication required")

    db = _db(request)
    row = (
        await db.execute(
            text(
                """
                SELECT c.id, c.short_id, c.title, c.status, c.severity,
                       c.opened_at, c.updated_at, c.closed_at, c.summary,
                       c.close_reason, c.tenant_id
                FROM investigations c WHERE c.id = :id
                """
            ),
            {"id": str(investigation_id)},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(404, "investigation not found")

    run = (
        await db.execute(
            text(
                """
                SELECT id, status, tokens_used, tokens_budget,
                       dollars_used, dollars_budget,
                       started_at, ended_at, last_error
                FROM investigation_runs WHERE investigation_id = :c
                ORDER BY started_at DESC LIMIT 1
                """
            ),
            {"c": str(investigation_id)},
        )
    ).mappings().first()

    counts = (
        await db.execute(
            text(
                """
                SELECT count(*) AS alert_count,
                       coalesce(sum(jsonb_array_length(initial_iocs)), 0) AS ioc_count
                FROM alerts WHERE investigation_id = :c
                """
            ),
            {"c": str(investigation_id)},
        )
    ).mappings().first()

    closed_at = row["closed_at"]
    opened_at = row["opened_at"]
    ttv = (
        (closed_at - opened_at).total_seconds()
        if closed_at and opened_at
        else None
    )

    disposition = _disposition(
        row["status"], run["status"] if run else None
    )
    verdict_decision = (
        "escalate"
        if disposition == "escalate"
        else "close" if disposition == "close_fp"
        else None
    )

    # Verdict reasoning, summary text, and per-run token spend are
    # MSSP-side operational data. Gate them on the user being any
    # tenant-side principal (customer_viewer or tenant_admin) — keying
    # off ``user_type`` rather than a single role name so future tenant
    # roles are covered automatically.
    user_type_str = (
        identity.user_type.value
        if hasattr(identity.user_type, "value")
        else str(identity.user_type)
    )
    is_customer = user_type_str == "tenant"

    return Investigation(
        id=str(row["id"]),
        title=row["title"] or row["short_id"],
        status=row["status"],
        phase=_phase_from_status(row["status"]),
        created_at=opened_at.isoformat(),
        updated_at=row["updated_at"].isoformat(),
        closed_at=closed_at.isoformat() if closed_at else None,
        alert_count=int(counts["alert_count"] or 0),
        observable_count=int(counts["ioc_count"] or 0),
        malicious_count=0,
        suspicious_count=0,
        clean_count=0,
        max_severity=_wazuh_severity_label(int(row["severity"] or 0)),
        verdict_decision=verdict_decision,
        thehive_case_id=None,
        time_to_triage_seconds=None,
        time_to_verdict_seconds=ttv,
        verdict_confidence=None,
        verdict_reasoning=None if is_customer else (row["close_reason"] or row["summary"]),
        threat_actor=None,
        tags=[],
        tokens_used=None if is_customer else (int(run["tokens_used"]) if run else None),
        tokens_budget=None if is_customer else (int(run["tokens_budget"]) if run else None),
        disposition=disposition,
        # The list has always carried this; the detail did not, which left the
        # UI unable to address any tenant-scoped action on an investigation --
        # the budget unlock (#127) needs it in the path.
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] else None,
        run_id=None if is_customer else (str(run["id"]) if run else None),
        dollars_used=(
            None if is_customer else (float(run["dollars_used"] or 0.0) if run else None)
        ),
        dollars_budget=(
            None if is_customer else (float(run["dollars_budget"] or 0.0) if run else None)
        ),
    )


class TimelineEvent(BaseModel):
    id: str
    investigation_id: str
    event_type: str
    timestamp: str
    data: dict[str, Any]
    # Replay/cursor fields (#72). Additive: the legacy timeline ignores them.
    seq: int | None = None
    run_id: str | None = None
    visibility: str | None = None


class EventTimelineResponse(BaseModel):
    events: list[TimelineEvent]
    # Legacy-timeline compatibility only (page length, not a grand total);
    # cursor clients use next_after_seq/has_more instead.
    total: int
    # Replay/cursor envelope (#72). ``server_now`` is the DB clock — the
    # authority the live head derives its offset from (never the browser's).
    server_now: str | None = None
    next_after_seq: int | None = None
    has_more: bool = False


@router.get("/{investigation_id}/events", response_model=EventTimelineResponse)
async def get_events(
    investigation_id: UUID,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    after_seq: int | None = Query(None, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> EventTimelineResponse:
    """Event feed, dual-natured (#72): with no cursor it behaves exactly as
    before (newest-first page for the timeline); with ``after_seq`` it is an
    ascending cursor feed for replay/live polling — ``next_after_seq`` is
    the next poll's cursor. RLS owns visibility filtering."""
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "authentication required")

    use_cursor = after_seq is not None
    effective_order = "asc" if use_cursor else order

    db = _db(request)
    rows = (
        await db.execute(
            text(
                f"""
                SELECT event_id, kind, payload, created_at, seq, run_id,
                       visibility, now() AS server_now
                FROM investigation_events
                WHERE investigation_id = :c
                  AND (CAST(:after_seq AS BIGINT) IS NULL OR seq > :after_seq)
                ORDER BY seq {"ASC" if effective_order == "asc" else "DESC"}
                LIMIT :limit
                """
            ),
            {
                "c": str(investigation_id),
                "limit": limit + 1,
                "after_seq": after_seq,
            },
        )
    ).mappings().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    server_now = (
        rows[0]["server_now"].isoformat()
        if rows
        else (await db.execute(text("SELECT now()"))).scalar_one().isoformat()
    )
    events = [
        TimelineEvent(
            id=str(r["event_id"]),
            investigation_id=str(investigation_id),
            event_type=r["kind"],
            timestamp=r["created_at"].isoformat(),
            data=dict(r["payload"]) if r["payload"] else {},
            seq=int(r["seq"]) if r["seq"] is not None else None,
            run_id=str(r["run_id"]) if r["run_id"] else None,
            visibility=r["visibility"],
        )
        for r in rows
    ]
    max_seq = max((e.seq for e in events if e.seq is not None), default=after_seq)
    return EventTimelineResponse(
        events=events,
        total=len(events),
        server_now=server_now,
        next_after_seq=max_seq,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# Lifecycle: cancel
#
# Pause/resume are intentionally not implemented: a status flip with no
# effect on the runs worker would be a button that lies about backend
# state (see issue #16). Cancel is a genuine terminal transition — the
# worker's claim query guards on ``investigations.status = 'active'``, so
# moving to ``cancelled`` stops any future run from being claimed, and we
# terminate the live run explicitly for a clean lifecycle (mirroring the
# merge-away path in ir/labels.py).
# ---------------------------------------------------------------------------


class CancelRequest(BaseModel):
    reason: str | None = None


class ActionResponse(BaseModel):
    success: bool
    message: str
    investigation_id: str


async def _resolve_tenant(db: AsyncSession, investigation_id: UUID) -> UUID:
    """Resolve an investigation's tenant_id. MSSP audience reads across
    tenants, so this succeeds with ``app.current_tenant_id`` unset; the
    caller then wraps the mutation in ``tenant_context`` so RLS WITH CHECK
    passes."""
    row = (
        await db.execute(
            text("SELECT tenant_id FROM investigations WHERE id = :id"),
            {"id": str(investigation_id)},
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "investigation not found")
    return UUID(str(row))


@router.post(
    "/{investigation_id}/cancel",
    response_model=ActionResponse,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN, Role.MSSP_MANAGER, Role.ANALYST))],
)
async def post_cancel_investigation(
    investigation_id: UUID, payload: CancelRequest, request: Request
) -> ActionResponse:
    db = _db(request)
    identity = current_identity(request)
    tid = await _resolve_tenant(db, investigation_id)
    reason = (payload.reason or "").strip() or None

    async with tenant_context(db, tid):
        try:
            await cancel_investigation(
                db,
                tenant_id=tid,
                investigation_id=investigation_id,
                reason=reason,
                actor=f"user:{identity.user_id}",
            )
        except LookupError:
            raise HTTPException(404, "investigation not found")
        except ValueError as exc:
            raise HTTPException(409, f"investigation {exc}")

    return ActionResponse(
        success=True,
        message="Investigation cancelled",
        investigation_id=str(investigation_id),
    )
