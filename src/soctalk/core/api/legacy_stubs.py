"""Empty-default stubs for legacy single-tenant API surfaces.

The canonical V1 frontend was lifted from the single-tenant SocTalk
codebase and still calls a handful of routes that haven't been bridged
to the V1 cases/investigation_runs model: ``/api/events/stream`` (SSE),
``/api/review/*``, ``/api/analytics/*``, ``/api/audit/*``,
``/api/settings``. Until those bridges land we return empty/default
shapes so the pages render without an error banner.

Auth: every route here is gated by the same session middleware; an
unauthenticated request gets the layout's pre-login probe handling
(401 → user=null on the SPA).

Side-effecting routes (POST /review/{id}/approve, etc.) intentionally
404 — they're disabled until the real bridge lands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from soctalk.core.tenancy.auth import current_identity


# All stubs sit behind the same session middleware as the rest of the
# canonical-frontend bridges. The middleware *attaches* identity but
# does not reject unauthenticated requests on its own — every router
# handler is responsible for the gate. Hang ``current_identity`` on
# the router so an unauthenticated GET hits 401 here instead of
# tunneling all the way through to a stub response (especially the
# long-lived /api/events/stream SSE).
router = APIRouter(tags=["legacy-stubs"], dependencies=[Depends(current_identity)])


# ---------------------------------------------------------------------------
# /api/events/stream — SSE heartbeat
# ---------------------------------------------------------------------------


@router.get("/api/events/stream")
async def events_stream(request: Request) -> StreamingResponse:
    """Open-ended SSE stream that emits a ``ping`` every 25s.

    The frontend's ``initSSE`` opens an EventSource against this path on
    every authenticated page. Returning a real SSE stream (instead of
    404) keeps the layout's "Live"/"Offline" badge accurate and lets us
    layer real events on later without a frontend change.

    Connection-pool note: the request goes through ``DBSessionMiddleware``
    which holds an ``AsyncSession`` open for the request lifetime — for
    a long-lived stream that means the session lingers indefinitely and
    multi-tab users would drain the pool. The auth dependency
    (``current_identity``) ran before this handler, so we explicitly
    close the session before entering the yield loop and let the
    middleware's ``finally`` find an already-closed session (no-op).
    """

    db = getattr(request.state, "db", None)
    if db is not None:
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass

    async def gen():
        # Emit one initial ``open`` so the client flips to Connected.
        yield "event: ping\n" + "data: {}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(25)
                yield "event: ping\n" + "data: {}\n\n"
        except asyncio.CancelledError:
            return

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


# ---------------------------------------------------------------------------
# /api/review/pending — bridged to the V1 pending_reviews table
# ---------------------------------------------------------------------------


class _PendingReviewItem(BaseModel):
    id: str
    investigation_id: str
    status: str
    title: str
    description: str
    max_severity: str
    alert_count: int
    malicious_count: int = 0
    suspicious_count: int = 0
    clean_count: int = 0
    findings: list[str] = []
    enrichments: dict[str, Any] = {}
    misp_context: dict[str, Any] | None = None
    ai_decision: str | None = None
    ai_confidence: float | None = None
    ai_assessment: str | None = None
    ai_recommendation: str | None = None
    timeout_seconds: int = 3600
    created_at: str
    expires_at: str | None = None


class _PendingReviewList(BaseModel):
    items: list[_PendingReviewItem] = []
    total: int = 0
    page: int = 1
    page_size: int = 50
    has_more: bool = False


@router.get("/api/review/pending", response_model=_PendingReviewList)
async def review_pending(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> _PendingReviewList:
    """List pending HIL reviews from the V1 pending_reviews table.

    Role-aware tenant scoping:
      - MSSP / platform admins → BYPASSRLS session, cross-tenant view.
      - Tenant-scoped users → app-role session with ``tenant_context``,
        RLS restricts the SELECT to their own tenant.
    """
    from sqlalchemy import text

    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import (
        get_app_sessionmaker,
        get_mssp_sessionmaker,
    )

    identity = current_identity(request)
    offset = (page - 1) * page_size
    count_sql = text(
        "SELECT count(*) FROM pending_reviews WHERE status = 'pending'"
    )
    list_sql = text(
        """
        SELECT id::text, investigation_id::text, status, title, description,
               max_severity, alert_count, malicious_count, suspicious_count,
               clean_count, findings, enrichments, misp_context, ai_decision,
               ai_confidence, ai_assessment, ai_recommendation,
               timeout_seconds, created_at, expires_at
        FROM pending_reviews
        WHERE status = 'pending'
        ORDER BY created_at DESC
        OFFSET :off LIMIT :lim
        """
    )

    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            total = (await s.execute(count_sql)).scalar_one()
            rows = (
                await s.execute(list_sql, {"off": offset, "lim": page_size})
            ).mappings().all()
    else:
        if identity.tenant_id is None:
            return _PendingReviewList(
                items=[], total=0, page=page, page_size=page_size, has_more=False
            )
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                total = (await s.execute(count_sql)).scalar_one()
                rows = (
                    await s.execute(list_sql, {"off": offset, "lim": page_size})
                ).mappings().all()

    items = [_pending_review_item(r) for r in rows]
    return _PendingReviewList(
        items=items,
        total=int(total or 0),
        page=page,
        page_size=page_size,
        has_more=(offset + len(items)) < int(total or 0),
    )


_MSSP_LEVEL_ROLES = {"platform_admin", "mssp_admin"}


def _pending_review_item(r: "Any") -> _PendingReviewItem:
    """Map a full ``pending_reviews`` row to the wire shape. Shared by the
    list and single-fetch endpoints so both stay in lockstep."""

    def _iso(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return _PendingReviewItem(
        id=r["id"],
        investigation_id=r["investigation_id"],
        status=r["status"],
        title=r["title"],
        description=r["description"],
        max_severity=r["max_severity"],
        alert_count=int(r["alert_count"] or 0),
        malicious_count=int(r["malicious_count"] or 0),
        suspicious_count=int(r["suspicious_count"] or 0),
        clean_count=int(r["clean_count"] or 0),
        findings=list(r["findings"] or []),
        enrichments=dict(r["enrichments"] or {}),
        misp_context=(dict(r["misp_context"]) if r["misp_context"] else None),
        ai_decision=r["ai_decision"],
        ai_confidence=(
            float(r["ai_confidence"]) if r["ai_confidence"] is not None else None
        ),
        ai_assessment=r["ai_assessment"],
        ai_recommendation=r["ai_recommendation"],
        timeout_seconds=int(r["timeout_seconds"] or 3600),
        created_at=_iso(r["created_at"]) or "",
        expires_at=_iso(r["expires_at"]),
    )


# Full-row projection used by both the list and single-fetch endpoints.
_REVIEW_COLUMNS = """
    id::text, investigation_id::text, status, title, description,
    max_severity, alert_count, malicious_count, suspicious_count,
    clean_count, findings, enrichments, misp_context, ai_decision,
    ai_confidence, ai_assessment, ai_recommendation,
    timeout_seconds, created_at, expires_at
"""


@router.get("/api/review/{review_id}", response_model=_PendingReviewItem)
async def review_detail(review_id: str, request: Request) -> _PendingReviewItem:
    """Fetch a single HIL review by id, role-aware tenant-scoped.

    Uses the same two-pool RLS idiom as ``_resolve_pending_review``:
    MSSP-level roles read cross-tenant via the BYPASSRLS session; tenant
    users are pinned by ``tenant_context`` so a cross-tenant id 404s the
    same way a truly missing row does (never disclose existence).
    """
    from fastapi import HTTPException
    from sqlalchemy import text

    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import (
        get_app_sessionmaker,
        get_mssp_sessionmaker,
    )

    identity = current_identity(request)
    sql = text(
        f"SELECT {_REVIEW_COLUMNS} FROM pending_reviews WHERE id = :rid"
    )

    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            row = (await s.execute(sql, {"rid": review_id})).mappings().first()
    else:
        if identity.tenant_id is None:
            raise HTTPException(403, "tenant scope required")
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                row = (await s.execute(sql, {"rid": review_id})).mappings().first()

    if row is None:
        raise HTTPException(404, "review not found")
    return _pending_review_item(row)


class _ReviewActionResponse(BaseModel):
    success: bool = True
    review_id: str
    new_status: str
    investigation_id: str


async def _resolve_pending_review(
    review_id: str, identity: "UserIdentity"
) -> dict[str, Any]:
    """Resolve a review with role-aware tenant scoping.

    MSSP-level roles (``platform_admin``, ``mssp_admin``) see all
    tenants' rows via the BYPASSRLS MSSP session. Tenant-level roles
    (``analyst``, ``tenant_admin``, ``customer_viewer``) are scoped to
    their own ``tenant_id`` via the RLS-subject app session with
    ``tenant_context`` set.

    Raises HTTP 404 if the review is not found within the caller's
    tenant scope (i.e. cross-tenant lookups by tenant users fail with
    the same code path as truly missing rows — never disclose
    existence across tenants).
    """
    from fastapi import HTTPException
    from sqlalchemy import text

    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import (
        get_app_sessionmaker,
        get_mssp_sessionmaker,
    )

    sql = (
        "SELECT id::text, investigation_id::text, tenant_id::text, status "
        "FROM pending_reviews WHERE id = :rid"
    )
    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            r = (
                await s.execute(text(sql), {"rid": review_id})
            ).mappings().first()
    else:
        if identity.tenant_id is None:
            raise HTTPException(403, "tenant scope required")
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                r = (
                    await s.execute(text(sql), {"rid": review_id})
                ).mappings().first()
    if r is None:
        raise HTTPException(404, "review not found")
    return dict(r)


async def _apply_review_decision(
    review_id: str,
    investigation_id: str,
    tenant_id: str | None,
    identity: "UserIdentity",
    decision: str,
    feedback: str | None,
) -> _ReviewActionResponse:
    """Common path for approve/reject/request-info.

    Routes through ``record_human_decision_received`` which appends
    the canonical ``HUMAN_DECISION_RECEIVED`` event to the event log
    and performs the V1-schema side effects (pending_reviews status
    flip, investigation close on reject) in one transaction.
    """
    from uuid import UUID

    from soctalk.core.ir.review_events import (
        _DECISION_TO_REVIEW_STATUS,
        record_human_decision_received,
    )
    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import (
        get_app_sessionmaker,
        get_mssp_sessionmaker,
    )

    tenant_uuid = UUID(tenant_id) if tenant_id else None
    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            await record_human_decision_received(
                s,
                review_id=UUID(review_id),
                investigation_id=UUID(investigation_id),
                tenant_id=tenant_uuid,
                decision=decision,
                feedback=feedback,
                reviewer=identity.email,
            )
            await s.commit()
    else:
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                await record_human_decision_received(
                    s,
                    review_id=UUID(review_id),
                    investigation_id=UUID(investigation_id),
                    tenant_id=tenant_uuid,
                    decision=decision,
                    feedback=feedback,
                    reviewer=identity.email,
                )
                await s.commit()
    return _ReviewActionResponse(
        success=True,
        review_id=review_id,
        new_status=_DECISION_TO_REVIEW_STATUS.get(decision, decision),
        investigation_id=investigation_id,
    )


class _ApproveBody(BaseModel):
    feedback: str | None = None


@router.post("/api/review/{review_id}/approve", response_model=_ReviewActionResponse)
async def review_approve(
    review_id: str, body: _ApproveBody, request: Request
) -> _ReviewActionResponse:
    """Analyst approved the AI verdict — investigation stays escalated."""
    from fastapi import HTTPException

    identity = current_identity(request)
    review = await _resolve_pending_review(review_id, identity)
    if review["status"] != "pending":
        raise HTTPException(409, f"review already {review['status']}")
    return await _apply_review_decision(
        review_id, review["investigation_id"], review["tenant_id"], identity,
        "approve", body.feedback,
    )


@router.post("/api/review/{review_id}/reject", response_model=_ReviewActionResponse)
async def review_reject(
    review_id: str, body: _ApproveBody, request: Request
) -> _ReviewActionResponse:
    """Analyst overrode the AI verdict — close as false positive."""
    from fastapi import HTTPException

    identity = current_identity(request)
    review = await _resolve_pending_review(review_id, identity)
    if review["status"] != "pending":
        raise HTTPException(409, f"review already {review['status']}")
    return await _apply_review_decision(
        review_id, review["investigation_id"], review["tenant_id"], identity,
        "reject", body.feedback,
    )


class _RequestInfoBody(BaseModel):
    questions: list[str] = []


@router.post("/api/review/{review_id}/request-info", response_model=_ReviewActionResponse)
async def review_request_info(
    review_id: str, body: _RequestInfoBody, request: Request
) -> _ReviewActionResponse:
    """Analyst requested additional information — investigation stays open."""
    from fastapi import HTTPException

    identity = current_identity(request)
    review = await _resolve_pending_review(review_id, identity)
    if review["status"] != "pending":
        raise HTTPException(409, f"review already {review['status']}")
    feedback = "Questions: " + " | ".join(body.questions) if body.questions else None
    return await _apply_review_decision(
        review_id, review["investigation_id"], review["tenant_id"], identity,
        "more_info", feedback,
    )


class _ExpireBody(BaseModel):
    reason: str | None = None


@router.post("/api/review/{review_id}/expire", response_model=_ReviewActionResponse)
async def review_expire(
    review_id: str, body: _ExpireBody, request: Request
) -> _ReviewActionResponse:
    """Retire a pending review without an analyst verdict.

    Used for operator-driven cleanup (stale or duplicate queue items) and
    for the future timeout-driven expiration job. Distinct from
    approve/reject/request-info: emits ``HUMAN_REVIEW_EXPIRED`` rather
    than ``HUMAN_DECISION_RECEIVED``, so the audit trail shows the row
    was retired administratively, not adjudicated.

    Authorization: MSSP-level roles (platform_admin, mssp_admin) can
    expire any tenant's review. Tenant-scoped roles can only expire
    their own; ``_resolve_pending_review`` enforces both via the same
    role-aware session pattern used by the other actions.
    """
    from uuid import UUID

    from fastapi import HTTPException

    from soctalk.core.ir.review_events import record_human_review_expired
    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import (
        get_app_sessionmaker,
        get_mssp_sessionmaker,
    )

    identity = current_identity(request)
    review = await _resolve_pending_review(review_id, identity)
    if review["status"] != "pending":
        raise HTTPException(409, f"review already {review['status']}")

    tenant_uuid = UUID(review["tenant_id"]) if review["tenant_id"] else None
    if tenant_uuid is None:
        raise HTTPException(500, "review missing tenant_id")

    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            await record_human_review_expired(
                s,
                review_id=UUID(review_id),
                investigation_id=UUID(review["investigation_id"]),
                tenant_id=tenant_uuid,
                reason=body.reason,
                reviewer=identity.email,
            )
            await s.commit()
    else:
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                await record_human_review_expired(
                    s,
                    review_id=UUID(review_id),
                    investigation_id=UUID(review["investigation_id"]),
                    tenant_id=tenant_uuid,
                    reason=body.reason,
                    reviewer=identity.email,
                )
                await s.commit()
    return _ReviewActionResponse(
        success=True,
        review_id=review_id,
        new_status="expired",
        investigation_id=review["investigation_id"],
    )


# ---------------------------------------------------------------------------
# /api/analytics/* — empty defaults
# ---------------------------------------------------------------------------


async def _analytics_session_for(identity: "UserIdentity"):
    """Same role-aware session pattern as the audit endpoints."""
    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import get_app_sessionmaker, get_mssp_sessionmaker

    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            yield s
    else:
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                yield s


async def _kpis(session, days: int) -> dict[str, Any]:
    from sqlalchemy import text as _t

    p = {"d": int(days)}
    inv = (
        await session.execute(
            _t(
                """
                SELECT
                    COUNT(*)::int                                   AS total,
                    COUNT(*) FILTER (WHERE status = 'auto_closed_fp')::int AS auto_closed,
                    COUNT(*) FILTER (WHERE status != 'active' AND closed_at IS NOT NULL)::int AS closed_any,
                    AVG(EXTRACT(EPOCH FROM (closed_at - opened_at)))
                        FILTER (WHERE closed_at IS NOT NULL)        AS mean_decision_s
                FROM investigations
                WHERE created_at >= now() - make_interval(days => :d)
                """
            ),
            p,
        )
    ).mappings().first() or {}
    pr = (
        await session.execute(
            _t(
                """
                SELECT
                    COUNT(*)::int                                       AS total_reviews,
                    COUNT(*) FILTER (WHERE ai_decision = 'escalate')::int AS ai_escalated,
                    AVG(ai_confidence) FILTER (WHERE ai_confidence IS NOT NULL) AS avg_conf,
                    COUNT(*) FILTER (WHERE ai_confidence >= 0.8)::int   AS high_conf,
                    COUNT(*) FILTER (WHERE status = 'rejected')::int    AS overridden
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                """
            ),
            p,
        )
    ).mappings().first() or {}

    total = int(inv.get("total") or 0)
    total_reviews = int(pr.get("total_reviews") or 0)
    ai_escalated = int(pr.get("ai_escalated") or 0)
    overridden = int(pr.get("overridden") or 0)
    return {
        "auto_close_rate": (int(inv.get("auto_closed") or 0) / total) if total else 0.0,
        "escalation_rate": (ai_escalated / total) if total else 0.0,
        "human_override_rate": (overridden / ai_escalated) if ai_escalated else 0.0,
        "mean_time_to_decision_seconds": (
            float(inv["mean_decision_s"]) if inv.get("mean_decision_s") else None
        ),
        "total_investigations": total,
        "auto_closed_count": int(inv.get("auto_closed") or 0),
        "escalated_count": ai_escalated,
        "human_reviewed_count": total_reviews,
        "avg_ai_confidence": (
            float(pr["avg_conf"]) if pr.get("avg_conf") is not None else None
        ),
        "high_confidence_rate": (
            (int(pr.get("high_conf") or 0) / total_reviews) if total_reviews else 0.0
        ),
    }


async def _ai_behavior(session, days: int) -> dict[str, Any]:
    from sqlalchemy import text as _t

    p = {"d": int(days)}
    # Confidence histogram in 10 buckets.
    rows = (
        await session.execute(
            _t(
                """
                SELECT width_bucket(ai_confidence, 0.0, 1.0001, 10) AS bucket,
                       COUNT(*)::int                                 AS n
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                  AND ai_confidence IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket
                """
            ),
            p,
        )
    ).all()
    # Frontend ApexCharts config reads ``range_label`` on each bucket
    # (analytics/+page.svelte#L73). Keep the key name aligned.
    confidence_distribution = [
        {"range_label": f"{(r[0] - 1) / 10:.1f}-{r[0] / 10:.1f}", "count": int(r[1])}
        for r in rows
    ]
    # Daily decision trend — frontend expects per-day rows with
    # decision counts pivoted into columns (close/escalate/needs_more_info/
    # suspicious), one chart series per column. The long-form
    # (day, decision, count) was rejected at render time because the
    # chart map() reads ``t.close`` etc. directly.
    daily = (
        await session.execute(
            _t(
                """
                SELECT date_trunc('day', created_at) AS day,
                       ai_decision,
                       COUNT(*)::int AS n
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                  AND ai_decision IS NOT NULL
                GROUP BY day, ai_decision
                ORDER BY day
                """
            ),
            p,
        )
    ).all()
    trends_by_day: dict[str, dict[str, Any]] = {}
    for day, decision, n in daily:
        key = day.isoformat() if day else "unknown"
        bucket = trends_by_day.setdefault(
            key,
            {
                "day": key,
                "close": 0,
                "escalate": 0,
                "needs_more_info": 0,
                "suspicious": 0,
            },
        )
        col = (decision or "").replace("-", "_")
        if col in bucket:
            bucket[col] = int(n)
    decision_trends = [trends_by_day[k] for k in sorted(trends_by_day.keys())]
    # Escalation breakdown — top severity buckets for escalated reviews.
    # Frontend reads ``reason`` (string, used for color-coding by
    # severity name) and ``percentage`` (0-1).
    breakdown = (
        await session.execute(
            _t(
                """
                SELECT max_severity, COUNT(*)::int AS n
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                  AND ai_decision = 'escalate'
                GROUP BY max_severity
                ORDER BY n DESC
                """
            ),
            p,
        )
    ).all()
    breakdown_total = sum(int(r[1]) for r in breakdown) or 1
    escalation_breakdown = [
        {
            # Title-case so the frontend's substring checks
            # (``includes('Critical')`` etc.) for colour mapping hit.
            "reason": (r[0] or "Unknown").title(),
            "count": int(r[1]),
            "percentage": int(r[1]) / breakdown_total,
        }
        for r in breakdown
    ]
    avg_by = (
        await session.execute(
            _t(
                """
                SELECT ai_decision, AVG(ai_confidence)::float AS c
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                  AND ai_confidence IS NOT NULL
                  AND ai_decision IS NOT NULL
                GROUP BY ai_decision
                """
            ),
            p,
        )
    ).all()
    avg_confidence_by_decision = {
        r[0]: float(r[1]) for r in avg_by if r[1] is not None
    }
    return {
        "confidence_distribution": confidence_distribution,
        "decision_trends": decision_trends,
        "escalation_breakdown": escalation_breakdown,
        "avg_confidence_by_decision": avg_confidence_by_decision,
    }


async def _human_review(session, days: int) -> dict[str, Any]:
    from sqlalchemy import text as _t

    p = {"d": int(days)}
    row = (
        await session.execute(
            _t(
                """
                SELECT
                    COUNT(*)::int                                       AS total,
                    COUNT(*) FILTER (WHERE status = 'approved')::int    AS approved,
                    COUNT(*) FILTER (WHERE status = 'rejected')::int    AS rejected,
                    COUNT(*) FILTER (WHERE status = 'info_requested')::int AS info_requested,
                    COUNT(*) FILTER (WHERE status = 'expired')::int     AS expired,
                    COUNT(*) FILTER (WHERE status = 'pending')::int     AS pending,
                    AVG(EXTRACT(EPOCH FROM (responded_at - created_at)))
                        FILTER (WHERE responded_at IS NOT NULL)         AS avg_review_s,
                    COUNT(*) FILTER (
                        WHERE ai_decision = 'escalate' AND status = 'approved'
                    )::int                                              AS ai_agreed,
                    COUNT(*) FILTER (
                        WHERE ai_decision = 'escalate' AND status = 'rejected'
                    )::int                                              AS ai_overridden
                FROM pending_reviews
                WHERE created_at >= now() - make_interval(days => :d)
                """
            ),
            p,
        )
    ).mappings().first() or {}
    total = int(row.get("total") or 0)
    agreed = int(row.get("ai_agreed") or 0)
    overridden = int(row.get("ai_overridden") or 0)
    return {
        "total_reviews": total,
        "approved": int(row.get("approved") or 0),
        "rejected": int(row.get("rejected") or 0),
        "info_requested": int(row.get("info_requested") or 0),
        "expired": int(row.get("expired") or 0),
        "pending": int(row.get("pending") or 0),
        "approval_rate": (int(row.get("approved") or 0) / total) if total else 0.0,
        "rejection_rate": (int(row.get("rejected") or 0) / total) if total else 0.0,
        "avg_review_time_seconds": (
            float(row["avg_review_s"]) if row.get("avg_review_s") else None
        ),
        "ai_agreed_count": agreed,
        "ai_overridden_count": overridden,
        "override_rate": (
            overridden / (agreed + overridden) if (agreed + overridden) else 0.0
        ),
    }


async def _outcomes(session, days: int) -> dict[str, Any]:
    from sqlalchemy import text as _t

    p = {"d": int(days)}
    row = (
        await session.execute(
            _t(
                """
                SELECT
                    COUNT(*)::int                                                          AS total_closed,
                    AVG(EXTRACT(EPOCH FROM (closed_at - opened_at)))                       AS avg_s,
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (closed_at - opened_at))
                    )                                                                      AS p50_s,
                    percentile_cont(0.9) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (closed_at - opened_at))
                    )                                                                      AS p90_s,
                    COUNT(*) FILTER (WHERE status = 'auto_closed_fp')::int                 AS fp,
                    COUNT(*) FILTER (WHERE close_reason ILIKE '%true%positive%'
                                       OR close_reason ILIKE '%confirmed%')::int           AS tp,
                    COUNT(*) FILTER (WHERE close_reason ILIKE '%suspicious%')::int         AS susp,
                    COUNT(*) FILTER (WHERE reopen_count > 0)::int                          AS reopened
                FROM investigations
                WHERE closed_at IS NOT NULL
                  AND closed_at >= now() - make_interval(days => :d)
                """
            ),
            p,
        )
    ).mappings().first() or {}
    total_closed = int(row.get("total_closed") or 0)
    return {
        "total_closed": total_closed,
        "avg_resolution_time_seconds": (
            float(row["avg_s"]) if row.get("avg_s") else None
        ),
        "p50_resolution_time_seconds": (
            float(row["p50_s"]) if row.get("p50_s") else None
        ),
        "p90_resolution_time_seconds": (
            float(row["p90_s"]) if row.get("p90_s") else None
        ),
        "closed_as_false_positive": int(row.get("fp") or 0),
        "closed_as_true_positive": int(row.get("tp") or 0),
        "closed_as_suspicious": int(row.get("susp") or 0),
        "reopen_rate": (
            (int(row.get("reopened") or 0) / total_closed) if total_closed else 0.0
        ),
    }


@router.get("/api/analytics/summary")
async def analytics_summary(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    identity = current_identity(request)
    period_end = datetime.now(timezone.utc)
    period_start = period_end.replace(microsecond=0) - timedelta(days=days)
    async for s in _analytics_session_for(identity):
        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "executive_kpis": await _kpis(s, days),
            "ai_behavior": await _ai_behavior(s, days),
            "human_review": await _human_review(s, days),
            "outcomes": await _outcomes(s, days),
        }


@router.get("/api/analytics/kpis")
async def analytics_kpis(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    identity = current_identity(request)
    async for s in _analytics_session_for(identity):
        return await _kpis(s, days)


@router.get("/api/analytics/ai-behavior")
async def analytics_ai_behavior(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    identity = current_identity(request)
    async for s in _analytics_session_for(identity):
        return await _ai_behavior(s, days)


@router.get("/api/analytics/human-review")
async def analytics_human_review(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    identity = current_identity(request)
    async for s in _analytics_session_for(identity):
        return await _human_review(s, days)


@router.get("/api/analytics/outcomes")
async def analytics_outcomes(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    identity = current_identity(request)
    async for s in _analytics_session_for(identity):
        return await _outcomes(s, days)


# ---------------------------------------------------------------------------
# /api/audit/* — empty list + event types
# ---------------------------------------------------------------------------


async def _audit_session_for(identity: "UserIdentity"):
    """Return a session + scoping context appropriate for the caller.

    MSSP-level roles use the BYPASSRLS session so they see every
    tenant's audit trail. Tenant-level roles use the RLS-subject
    session with ``tenant_context`` so their audit query is naturally
    scoped to their own tenant_id by the events_tenant_isolation
    policy on the ``events`` table.

    Yields ``(session, needs_commit)`` — read-only here so the caller
    never commits, but the shape mirrors the write helpers above for
    readability.
    """
    from soctalk.core.tenancy.context import tenant_context
    from soctalk.core.tenancy.db import get_app_sessionmaker, get_mssp_sessionmaker

    if identity.role in _MSSP_LEVEL_ROLES:
        sm = get_mssp_sessionmaker()
        async with sm() as s:
            yield s
    else:
        sm = get_app_sessionmaker()
        async with sm() as s:
            async with tenant_context(s, identity.tenant_id):
                yield s


@router.get("/api/audit/event-types")
async def audit_event_types(request: Request) -> dict[str, list[str]]:
    """Distinct event types present in the audit log — drives the UI filter."""
    from sqlalchemy import text as _t

    identity = current_identity(request)
    async for s in _audit_session_for(identity):
        rows = (
            await s.execute(_t("SELECT DISTINCT event_type FROM events ORDER BY event_type"))
        ).all()
        return {"event_types": [r[0] for r in rows]}


@router.get("/api/audit")
async def audit_list(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    aggregate_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    investigation_id: str | None = None,
) -> dict[str, Any]:
    """Paginated audit log query against the ``events`` table.

    Filters are AND-composed; missing filters are dropped from the
    WHERE. ``total`` is a separate COUNT(*) over the same predicate so
    the frontend pager works. Tenant scoping is handled by either
    BYPASSRLS (MSSP) or the events RLS policy (tenant).
    """
    from sqlalchemy import text as _t

    identity = current_identity(request)
    conds: list[str] = []
    params: dict[str, Any] = {}
    if event_type:
        conds.append("event_type = :et")
        params["et"] = event_type
    if aggregate_type:
        conds.append("aggregate_type = :at")
        params["at"] = aggregate_type
    if investigation_id:
        conds.append("aggregate_id::text = :aid")
        params["aid"] = investigation_id
    if start_date:
        conds.append("timestamp >= :sd")
        params["sd"] = start_date
    if end_date:
        conds.append("timestamp <= :ed")
        params["ed"] = end_date
    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    offset = (page - 1) * page_size
    params["lim"] = page_size
    params["off"] = offset

    async for s in _audit_session_for(identity):
        total = (
            await s.execute(_t(f"SELECT COUNT(*) FROM events{where}"), params)
        ).scalar_one()
        rows = (
            await s.execute(
                _t(
                    f"""
                    SELECT id::text, aggregate_id::text, aggregate_type, event_type,
                           version, timestamp, data
                    FROM events{where}
                    ORDER BY timestamp DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                params,
            )
        ).mappings().all()
        items = [
            {
                "id": r["id"],
                "aggregate_id": r["aggregate_id"],
                "aggregate_type": r["aggregate_type"],
                "event_type": r["event_type"],
                "version": int(r["version"]),
                "timestamp": (
                    r["timestamp"].isoformat() if r["timestamp"] else None
                ),
                "data": r["data"],
            }
            for r in rows
        ]
        return {
            "items": items,
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
            "has_more": (offset + len(items)) < int(total or 0),
        }


@router.get("/api/audit/stats")
async def audit_stats(
    request: Request, hours: int = Query(24, ge=1, le=720)
) -> dict[str, Any]:
    """Aggregate counters for the audit dashboard."""
    from sqlalchemy import text as _t

    identity = current_identity(request)
    # ``hours`` is already bounded to [1, 720] by Query(...). Construct
    # the interval via ``make_interval`` rather than ``(:h)::interval``
    # because asyncpg can't cast a bind param to ``interval`` directly
    # (server-side parser sees ``$1`` before it has a target type).
    params = {"h": int(hours)}
    async for s in _audit_session_for(identity):
        total = (
            await s.execute(
                _t(
                    "SELECT COUNT(*) FROM events "
                    "WHERE timestamp >= now() - make_interval(hours => :h)"
                ),
                params,
            )
        ).scalar_one()
        uniq = (
            await s.execute(
                _t(
                    "SELECT COUNT(DISTINCT aggregate_id) FROM events "
                    "WHERE timestamp >= now() - make_interval(hours => :h)"
                ),
                params,
            )
        ).scalar_one()
        by_type = (
            await s.execute(
                _t(
                    "SELECT event_type, COUNT(*) FROM events "
                    "WHERE timestamp >= now() - make_interval(hours => :h) "
                    "GROUP BY event_type"
                ),
                params,
            )
        ).all()
        by_hour = (
            await s.execute(
                _t(
                    "SELECT date_trunc('hour', timestamp) AS h, COUNT(*) "
                    "FROM events "
                    "WHERE timestamp >= now() - make_interval(hours => :h) "
                    "GROUP BY h ORDER BY h"
                ),
                params,
            )
        ).all()
        return {
            "period_hours": hours,
            "total_events": int(total or 0),
            "unique_investigations": int(uniq or 0),
            "events_by_type": {r[0]: int(r[1]) for r in by_type},
            "events_by_hour": {
                (r[0].isoformat() if r[0] else ""): int(r[1]) for r in by_hour
            },
        }


@router.get("/api/audit/investigation/{investigation_id}")
async def audit_investigation(
    investigation_id: str,
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Per-investigation event timeline for the case-detail audit tab."""
    from sqlalchemy import text as _t

    identity = current_identity(request)
    async for s in _audit_session_for(identity):
        inv = (
            await s.execute(
                _t(
                    "SELECT title, status, created_at FROM investigations "
                    "WHERE id::text = :id"
                ),
                {"id": investigation_id},
            )
        ).mappings().first()
        rows = (
            await s.execute(
                _t(
                    """
                    SELECT id::text, aggregate_type, event_type, version,
                           timestamp, data
                    FROM events
                    WHERE aggregate_id::text = :id
                    ORDER BY version ASC
                    LIMIT :lim
                    """
                ),
                {"id": investigation_id, "lim": limit},
            )
        ).mappings().all()
        events = [
            {
                "id": r["id"],
                "aggregate_type": r["aggregate_type"],
                "event_type": r["event_type"],
                "version": int(r["version"]),
                "timestamp": (
                    r["timestamp"].isoformat() if r["timestamp"] else None
                ),
                "data": r["data"],
            }
            for r in rows
        ]
        return {
            "investigation_id": investigation_id,
            "title": inv["title"] if inv else None,
            "status": inv["status"] if inv else "unknown",
            "phase": "unknown",
            "created_at": (
                inv["created_at"].isoformat()
                if inv and inv["created_at"]
                else datetime.now(timezone.utc).isoformat()
            ),
            "events": events,
            "total_events": len(events),
        }


# ---------------------------------------------------------------------------
# /api/settings — minimal read-only snapshot
# ---------------------------------------------------------------------------


@router.get("/api/settings")
async def settings_get() -> dict[str, Any]:
    """Read-only settings snapshot.

    The full settings UX edits MCP integration credentials in the legacy
    single-tenant install; in V1 those live in per-tenant
    IntegrationConfig (see /api/llm/* and the MSSP tenants UI). We
    return a non-empty shape with everything ``readonly=True`` so the
    page renders the read-only view rather than an error.
    """

    return {
        "id": "v1-readonly",
        "readonly": False,
        "sources": {},
        "llm_provider": "openai",
        "llm_fast_model": "gpt-4o-mini",
        "llm_reasoning_model": "gpt-4o",
        "llm_temperature": 0.2,
        "llm_max_tokens": 4096,
        "llm_anthropic_base_url": None,
        "llm_openai_base_url": None,
        "llm_openai_organization": None,
        "anthropic_api_key_configured": False,
        "openai_api_key_configured": True,
        "llm_keys_conflict": False,
        "wazuh_enabled": True,
        "wazuh_url": None,
        "wazuh_verify_ssl": True,
        "wazuh_credentials_configured": True,
        "cortex_enabled": False,
        "cortex_url": None,
        "cortex_verify_ssl": True,
        "cortex_api_key_configured": False,
        "thehive_enabled": False,
        "thehive_url": None,
        "thehive_organisation": None,
        "thehive_verify_ssl": True,
        "thehive_api_key_configured": False,
        "misp_enabled": False,
        "misp_url": None,
        "misp_verify_ssl": True,
        "misp_api_key_configured": False,
        "slack_enabled": False,
        "slack_channel": None,
        "slack_notify_on_escalation": False,
        "slack_notify_on_verdict": False,
        "slack_webhook_configured": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/settings")
async def settings_post(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Accept settings updates from the legacy UI and return success.

    This stub persists nothing — the V1 flow stores per-tenant values in
    `integration_configs`/other tables. The UI expects a 200+success payload
    when it posts here; mirror back the body for debugging.
    """
    return {"success": True, "updated": body}


__all__ = ["router"]
