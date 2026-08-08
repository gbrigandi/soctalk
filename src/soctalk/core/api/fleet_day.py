"""Fleet-day aggregate for the flight recorder (issue #72, Phase 3 data).

One day of the pipeline at a glance: exact counters classified from
EXPLICIT replay-event payloads (terminal-close ``path``, guard
``effect``), escalations from ``pending_reviews.ai_decision`` (the same
signal the existing analytics use), plus a deterministic sample of real
alert "dots" for the fleet map — every dot carries a real alert id and
investigation id; ``sample_rate`` is disclosed so the UI can say
"showing 1 in N". Counters never come from the sample.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as date_type
from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from soctalk.core.tenancy.auth import current_identity
from soctalk.core.tenancy.context import tenant_context
from soctalk.core.tenancy.db import get_app_sessionmaker
from soctalk.core.tenancy.models import UserType

# NOTE on visibility: these endpoints run on a fresh session whose
# TenantContext defaults audience='mssp', so the panel's AGGREGATES are
# mssp-scope — deliberate for now, because the adapter stamps every alert
# and every ingest-band investigation ``mssp_only`` and a strict
# customer-audience read would blank the tenant fleet panel entirely.
# Drill LINKS are different: the tenant's own detail page runs under the
# request session's customer audience, so a link to an ``mssp_only``
# investigation 404s. The queries below therefore null the link unless
# the CALLER's audience can actually open the target.


def _caller_is_mssp(identity: Any) -> bool:
    return identity.user_type != UserType.TENANT.value

logger = structlog.get_logger()

router = APIRouter(tags=["fleet-day"], dependencies=[Depends(current_identity)])


def _resolve_day_window(
    tz: str, date: date_type | None
) -> tuple[date_type, datetime, datetime]:
    """Local-day window [start, end) for a tz; today when date is None."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        # A bad zone key or malformed string is a client problem (400). A
        # corrupt tz database or import failure is not, so let anything else
        # surface as a 500 rather than mislabel a server fault as user error.
        raise HTTPException(400, f"unknown timezone: {tz}") from None
    day = date or datetime.now(zone).date()
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    return day, start, start + timedelta(days=1)


def _hour_histogram(timestamps: Iterable[datetime], zone: tzinfo) -> list[int]:
    """24 local-hour buckets for aware timestamps, bucketed in Python.

    Done here rather than via SQL ``extract(hour FROM ts AT TIME ZONE :tz)``
    because Postgres' tz catalog rejects legacy IANA backward aliases (e.g.
    ``America/Buenos_Aires``) that Python's bundled ``tzdata`` resolves. A
    browser reporting such a legacy zone would otherwise pass the Python
    validation above and then 500 in the histogram query. Converting here keeps
    the tzdata dependency the single source of zone truth for the endpoint.
    """
    hist = [0] * 24
    for ts in timestamps:
        hist[ts.astimezone(zone).hour] += 1
    return hist


async def _close_counts(db: Any, p: dict[str, Any]) -> dict[str, int]:
    """Terminal-close counts by disposition path within the window."""
    rows = (
        await db.execute(
            text(
                """
                SELECT payload->>'path' AS path, COUNT(*)::int AS n
                FROM investigation_events
                WHERE kind = 'auto_closed'
                  AND created_at >= :s AND created_at < :e
                GROUP BY path
                """
            ),
            p,
        )
    ).mappings().all()
    return {r["path"]: int(r["n"]) for r in rows}


async def _guard_veto_count(db: Any, p: dict[str, Any]) -> int:
    return int(
        (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM investigation_events
                    WHERE kind = 'guard_evaluated'
                      AND payload->>'effect' = 'override'
                      AND created_at >= :s AND created_at < :e
                    """
                ),
                p,
            )
        ).mappings().one()["n"]
    )


async def _escalated_count(db: Any, p: dict[str, Any]) -> int:
    # pending_reviews.created_at is a NAIVE timestamp (UTC by convention),
    # unlike alerts/investigation_events which are timestamptz — normalize
    # the aware window params in SQL or asyncpg refuses the comparison.
    return int(
        (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM pending_reviews
                    WHERE ai_decision = 'escalate'
                      AND created_at >= (CAST(:s AS timestamptz) AT TIME ZONE 'UTC')
                      AND created_at <  (CAST(:e AS timestamptz) AT TIME ZONE 'UTC')
                    """
                ),
                p,
            )
        ).mappings().one()["n"]
    )


class FleetDot(BaseModel):
    alert_id: str
    investigation_id: str | None
    first_event_at: str
    closed_at: str | None
    path: str | None
    outcome: str
    veto: bool


class FleetVetoRow(BaseModel):
    investigation_id: str
    at: str
    stage: str | None
    fired: list[str]


class FleetDayResponse(BaseModel):
    date: str
    tz: str
    server_now: str
    window_start: str
    window_end: str
    ingested: int
    closed_ingest_memoized: int
    closed_ingest_rules: int
    closed_operational: int
    closed_reasoning: int
    escalated: int
    guard_vetoes: int
    still_open: int
    ingest_histogram: list[int]  # 24 hourly buckets, local tz
    dollars_used: float
    tokens_used: int
    sample_rate: float
    dots: list[FleetDot]
    recent_vetoes: list[FleetVetoRow]


class FleetArrival(BaseModel):
    alert_id: str
    investigation_id: str | None
    first_event_at: str
    status: str | None


class FleetLiveResponse(BaseModel):
    server_now: str
    window_start: str
    ingested: int
    closed_ingest_memoized: int
    closed_ingest_rules: int
    closed_operational: int
    closed_reasoning: int
    escalated: int
    guard_vetoes: int
    in_flight: int
    last_alert_at: str | None
    # Stage → count of open investigations parked there, derived from each
    # open investigation's LATEST replay beat. Investigations with no
    # recorded beat land in "unknown" — never a fake parked node.
    open_by_stage: dict[str, int]
    # UNSAMPLED newest arrivals: the sampled dot set can miss or displace a
    # just-arrived alert (md5 ordering), so live arrivals get their own feed.
    recent_arrivals: list[FleetArrival]


_WORKER_STAGE = {
    "wazuh": "wazuh",
    "cortex": "cortex",
    "misp": "misp",
    "authorization_context": "authz",
    "thehive": "thehive",
}


def stage_for_latest_event(kind: str, payload: dict[str, Any] | None) -> str:
    """Map an open investigation's latest replay beat to a map stage.

    Pure and unit-tested. ``worker_result`` parks back at the supervisor
    (the worker returned); unknown kinds are honestly ``unknown``.
    """
    p = payload or {}
    if kind in ("alert_ingested", "policy_resolved"):
        return "gate"
    if kind == "supervisor_decision":
        return "sup"
    if kind == "worker_started":
        return _WORKER_STAGE.get(str(p.get("worker") or ""), "sup")
    if kind == "worker_result":
        return "sup"
    if kind == "budget_warning":
        # A soft budget warning fires between supervisor cycles; the run keeps
        # going through the supervisor. Park it there, not in "unknown" (#103).
        return "sup"
    if kind == "verdict_rendered":
        return "verdict"
    if kind == "guard_evaluated":
        return "guard"
    if kind in ("human_review_requested", "human_decision"):
        return "human"
    if kind == "auto_closed":
        return "close"
    return "unknown"


@router.get("/api/analytics/fleet-live", response_model=FleetLiveResponse)
async def fleet_live(
    request: Request,
    tz: str = Query("UTC", max_length=64),
    arrivals_window_s: int = Query(900, ge=60, le=3600),
    arrivals_limit: int = Query(30, ge=1, le=100),
) -> FleetLiveResponse:
    """Lightweight live snapshot for the fleet panel (polled at 5-10s).

    Exact today-so-far counters plus the quiet-state facts that make a
    still map legible: last arrival, in-flight count, and open
    investigations grouped by the stage their latest replay beat parks
    them at. Heavier day context (dots, histogram) stays on fleet-day.
    """
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "authentication required")
    # Tenant-bound users carry tenant_id; pinned MSSP sessions carry the
    # impersonated tenant in current_tenant. Either scopes the panel.
    tenant_id = identity.tenant_id or identity.current_tenant
    if tenant_id is None:
        raise HTTPException(403, "tenant scope required")

    _, start, end = _resolve_day_window(tz, None)

    sm = get_app_sessionmaker()
    async with sm() as db, tenant_context(db, tenant_id):
        p: dict[str, Any] = {"s": start, "e": end, "mssp": _caller_is_mssp(identity)}
        server_now = (await db.execute(text("SELECT now()"))).scalar_one()

        alerts_row = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS ingested, MAX(first_event_at) AS last_at
                    FROM alerts
                    WHERE first_event_at >= :s AND first_event_at < :e
                    """
                ),
                p,
            )
        ).mappings().one()

        closes = await _close_counts(db, p)
        vetoes = await _guard_veto_count(db, p)
        escalated = await _escalated_count(db, p)

        in_flight_row = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM investigations
                    WHERE status = 'active'
                      AND opened_at >= :s AND opened_at < :e
                    """
                ),
                p,
            )
        ).mappings().one()
        in_flight = int(in_flight_row["n"])

        latest_rows = (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT ON (ev.investigation_id)
                           ev.investigation_id, ev.kind, ev.payload
                    FROM investigation_events ev
                    JOIN investigations i ON i.id = ev.investigation_id
                    WHERE i.status = 'active'
                      AND i.opened_at >= :s AND i.opened_at < :e
                    ORDER BY ev.investigation_id, ev.seq DESC
                    """
                ),
                p,
            )
        ).mappings().all()
        open_by_stage: dict[str, int] = {}
        for r in latest_rows:
            stage = stage_for_latest_event(
                str(r["kind"]), dict(r["payload"]) if r["payload"] else None
            )
            open_by_stage[stage] = open_by_stage.get(stage, 0) + 1
        staged = sum(open_by_stage.values())
        if in_flight > staged:
            open_by_stage["unknown"] = open_by_stage.get("unknown", 0) + (
                in_flight - staged
            )

        arrival_rows = (
            await db.execute(
                text(
                    """
                    SELECT a.id,
                           -- Same caller-audience link gate as fleet-day dots.
                           CASE WHEN :mssp
                                  OR i.visibility IN ('customer_safe', 'system')
                                THEN i.id END AS investigation_id,
                           a.first_event_at,
                           a.status
                    FROM alerts a
                    LEFT JOIN investigations i ON i.id = a.investigation_id
                    WHERE a.first_event_at >= :recent AND a.first_event_at < :e
                    ORDER BY a.first_event_at DESC
                    LIMIT :lim
                    """
                ),
                {
                    **p,
                    "recent": server_now - timedelta(seconds=arrivals_window_s),
                    "lim": arrivals_limit,
                },
            )
        ).mappings().all()

    return FleetLiveResponse(
        server_now=server_now.isoformat(),
        window_start=start.isoformat(),
        ingested=int(alerts_row["ingested"]),
        closed_ingest_memoized=closes.get("ingest_memoized", 0),
        closed_ingest_rules=closes.get("ingest_rules", 0),
        closed_operational=closes.get("operational", 0),
        closed_reasoning=closes.get("reasoning", 0),
        escalated=escalated,
        guard_vetoes=vetoes,
        in_flight=in_flight,
        last_alert_at=(
            alerts_row["last_at"].isoformat() if alerts_row["last_at"] else None
        ),
        open_by_stage=open_by_stage,
        recent_arrivals=[
            FleetArrival(
                alert_id=str(r["id"]),
                investigation_id=(
                    str(r["investigation_id"]) if r["investigation_id"] else None
                ),
                first_event_at=r["first_event_at"].isoformat(),
                status=r["status"],
            )
            for r in arrival_rows
        ],
    )


@router.get("/api/analytics/fleet-day", response_model=FleetDayResponse)
async def fleet_day(
    request: Request,
    date: date_type | None = Query(None, description="Local date; default today"),
    tz: str = Query("UTC", max_length=64),
    sample_limit: int = Query(500, ge=1, le=2000),
    fallback: str | None = Query(
        None,
        description=(
            "'latest_active': when TODAY (date omitted) has zero alerts, "
            "serve the most recent local day that has any, up to 30 days "
            "back. Never applies to an explicit date — an empty requested "
            "day is the honest answer. The response's 'date' self-describes "
            "any substitution."
        ),
    ),
) -> FleetDayResponse:
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(401, "authentication required")
    # Tenant-bound users carry tenant_id; pinned MSSP sessions carry the
    # impersonated tenant in current_tenant. Either scopes the panel.
    tenant_id = identity.tenant_id or identity.current_tenant
    if tenant_id is None:
        raise HTTPException(403, "tenant scope required")

    day, start, end = _resolve_day_window(tz, date)
    zone = start.tzinfo  # the resolved (tzdata-backed) ZoneInfo; reused below

    sm = get_app_sessionmaker()
    async with sm() as db, tenant_context(db, tenant_id):
        p: dict[str, Any] = {"s": start, "e": end, "mssp": _caller_is_mssp(identity)}

        server_now = (await db.execute(text("SELECT now()"))).scalar_one()

        # Latest-active-day fallback (Codex-adjudicated, zero-only rule):
        # sparse-but-nonzero today ALWAYS wins — one alert means the day
        # has evidence; substitution on any threshold would be a hidden
        # editorial model. 'Active' is alert-anchored (the film and map
        # are built from arrivals, never closes/vetoes/spend).
        if fallback == "latest_active" and date is None:
            today_n = (
                await db.execute(
                    text(
                        """
                        SELECT COUNT(*)::int AS n FROM alerts
                        WHERE first_event_at >= :s AND first_event_at < :e
                        """
                    ),
                    p,
                )
            ).mappings().one()["n"]
            if int(today_n) == 0:
                last_at = (
                    await db.execute(
                        text(
                            """
                            SELECT MAX(first_event_at) AS t FROM alerts
                            -- Anchor the 30-day lookback to today's local
                            -- midnight (:s), not now(): the eligible range
                            -- must not shrink as the day progresses.
                            WHERE first_event_at >= CAST(:s AS timestamptz)
                                                      - interval '30 days'
                              AND first_event_at < :s
                            """
                        ),
                        p,
                    )
                ).mappings().one()["t"]
                if last_at is not None:
                    day, start, end = _resolve_day_window(
                        tz, last_at.astimezone(ZoneInfo(tz)).date()
                    )
                    p["s"], p["e"] = start, end

        alerts_row = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS ingested
                    FROM alerts
                    WHERE first_event_at >= :s AND first_event_at < :e
                    """
                ),
                p,
            )
        ).mappings().one()

        # Local-hour histogram bucketed in Python from the resolved zone (see
        # _hour_histogram): Postgres' catalog rejects legacy tz aliases, so the
        # bucketing must not be done in SQL with the request timezone.
        ts_rows = (
            await db.execute(
                text(
                    """
                    SELECT first_event_at
                    FROM alerts
                    WHERE first_event_at >= :s AND first_event_at < :e
                    """
                ),
                p,
            )
        ).scalars().all()
        histogram = _hour_histogram(ts_rows, zone)

        closes = await _close_counts(db, p)
        vetoes = await _guard_veto_count(db, p)
        escalated = await _escalated_count(db, p)

        recent_veto_rows = (
            await db.execute(
                text(
                    """
                    SELECT investigation_id, created_at, payload
                    FROM investigation_events
                    WHERE kind = 'guard_evaluated'
                      AND payload->>'effect' = 'override'
                      AND created_at >= :s AND created_at < :e
                    ORDER BY created_at DESC
                    LIMIT 8
                    """
                ),
                p,
            )
        ).mappings().all()

        spend_row = (
            await db.execute(
                text(
                    """
                    SELECT COALESCE(SUM(dollars_used), 0)::float AS dollars,
                           COALESCE(SUM(tokens_used), 0)::bigint AS tokens
                    FROM investigation_runs
                    WHERE started_at >= :s AND started_at < :e
                    """
                ),
                p,
            )
        ).mappings().one()

        # Deterministic dot sample: md5-ordered so repeated polls return the
        # same alerts (a live view must not reshuffle its dots).
        dot_rows = (
            await db.execute(
                text(
                    """
                    SELECT a.id AS alert_id,
                           -- Drill-down link ONLY when the caller can actually
                           -- open the investigation: a tenant session's detail
                           -- page hides mssp_only rows, so linking one 404s.
                           CASE WHEN :mssp
                                  OR i.visibility IN ('customer_safe', 'system')
                                THEN i.id END AS investigation_id,
                           a.first_event_at,
                           i.closed_at,
                           i.status AS inv_status,
                           ce.payload->>'path' AS path,
                           EXISTS (
                             SELECT 1 FROM investigation_events ge
                             WHERE ge.investigation_id = a.investigation_id
                               AND ge.kind = 'guard_evaluated'
                               AND ge.payload->>'effect' = 'override'
                           ) AS veto,
                           EXISTS (
                             SELECT 1 FROM pending_reviews pr
                             WHERE pr.investigation_id = a.investigation_id
                               AND pr.ai_decision = 'escalate'
                           ) AS escalated
                    FROM alerts a
                    LEFT JOIN investigations i ON i.id = a.investigation_id
                    LEFT JOIN LATERAL (
                      SELECT payload FROM investigation_events ev
                      WHERE ev.investigation_id = a.investigation_id
                        AND ev.kind = 'auto_closed'
                      ORDER BY ev.seq DESC LIMIT 1
                    ) ce ON true
                    WHERE a.first_event_at >= :s AND a.first_event_at < :e
                    ORDER BY md5(a.id::text)
                    LIMIT :lim
                    """
                ),
                {**p, "lim": sample_limit},
            )
        ).mappings().all()

        dots = []
        for r in dot_rows:
            if r["path"]:
                outcome = "closed"
            elif r["escalated"]:
                outcome = "human"
            elif r["inv_status"] == "auto_closed_fp":
                # Terminal row without a replay beat: closed before Phase 0
                # instrumentation existed. Honest label, not a guess.
                outcome = "closed_unrecorded"
            else:
                outcome = "open"
            dots.append(
                FleetDot(
                    alert_id=str(r["alert_id"]),
                    investigation_id=(
                        str(r["investigation_id"]) if r["investigation_id"] else None
                    ),
                    first_event_at=r["first_event_at"].isoformat(),
                    closed_at=r["closed_at"].isoformat() if r["closed_at"] else None,
                    path=r["path"],
                    outcome=outcome,
                    veto=bool(r["veto"]),
                )
            )

    ingested = int(alerts_row["ingested"])
    closed_total = sum(closes.values())
    return FleetDayResponse(
        date=day.isoformat(),
        tz=tz,
        server_now=server_now.isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        ingested=ingested,
        closed_ingest_memoized=closes.get("ingest_memoized", 0),
        closed_ingest_rules=closes.get("ingest_rules", 0),
        closed_operational=closes.get("operational", 0),
        closed_reasoning=closes.get("reasoning", 0),
        escalated=escalated,
        guard_vetoes=vetoes,
        still_open=max(0, ingested - closed_total - escalated),
        ingest_histogram=histogram,
        dollars_used=float(spend_row["dollars"]),
        tokens_used=int(spend_row["tokens"]),
        sample_rate=(min(1.0, sample_limit / ingested) if ingested else 1.0),
        dots=dots,
        recent_vetoes=[
            FleetVetoRow(
                investigation_id=str(r["investigation_id"]),
                at=r["created_at"].isoformat(),
                stage=(r["payload"] or {}).get("stage"),
                fired=list((r["payload"] or {}).get("fired") or []),
            )
            for r in recent_veto_rows
        ],
    )
