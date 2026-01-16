"""Dashboard metrics and statistics endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from soctalk.api.deps import DbSession
from soctalk.persistence.models import (
    AnalyzerStats,
    InvestigationReadModel,
    IOCStats,
    MetricsHourly,
    RuleStats,
)

logger = structlog.get_logger()

router = APIRouter(tags=["metrics"])


# Response models
class MetricsOverview(BaseModel):
    """Overview metrics for the dashboard."""

    # Current counts
    open_investigations: int
    pending_reviews: int

    # Today's metrics
    investigations_created_today: int
    investigations_closed_today: int
    escalations_today: int
    auto_closed_today: int

    # Time metrics (averages)
    avg_time_to_triage_seconds: int | None
    avg_time_to_verdict_seconds: int | None

    # Totals
    total_alerts_today: int
    total_observables_today: int
    malicious_observables_today: int

    # Severity breakdown
    severity_breakdown: dict[str, int]

    # Verdict breakdown
    verdict_breakdown: dict[str, int]


class HourlyMetric(BaseModel):
    """Single hour of metrics."""

    hour: datetime
    investigations_created: int
    investigations_closed: int
    escalations: int
    auto_closed: int
    avg_time_to_verdict_seconds: int | None
    total_alerts: int
    total_observables: int
    malicious_observables: int
    open_wip: int  # Open investigations at end of this hour


class HourlyMetricsResponse(BaseModel):
    """Hourly metrics response."""

    metrics: list[HourlyMetric]
    start: datetime
    end: datetime
    total_hours: int


class IOCStatItem(BaseModel):
    """IOC statistics item."""

    id: UUID
    value: str
    type: str
    times_seen: int
    last_seen: datetime
    malicious_count: int
    benign_count: int
    threat_actors: list[str]
    malicious_rate: float


class IOCStatsResponse(BaseModel):
    """IOC statistics response."""

    items: list[IOCStatItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class RuleStatItem(BaseModel):
    """Rule statistics item."""

    rule_id: str
    times_triggered: int
    escalation_count: int
    auto_close_count: int
    precision_rate: float | None
    escalation_rate: float


class RuleStatsResponse(BaseModel):
    """Rule statistics response."""

    items: list[RuleStatItem]
    total: int


class AnalyzerStatItem(BaseModel):
    """Analyzer statistics item."""

    analyzer: str
    invocations: int
    successes: int
    failures: int
    avg_response_time_ms: float | None
    success_rate: float


class AnalyzerStatsResponse(BaseModel):
    """Analyzer statistics response."""

    items: list[AnalyzerStatItem]
    total: int


@router.get("/metrics/overview", response_model=MetricsOverview)
async def get_metrics_overview(
    db: DbSession,
) -> MetricsOverview:
    """Get overview metrics for the dashboard.

    Returns aggregate metrics including open investigations, pending reviews,
    today's activity, and breakdowns by severity and verdict.

    Args:
        db: Database session.

    Returns:
        Overview metrics for the dashboard.
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Count open investigations
    open_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.status.in_(["pending", "in_progress", "paused"])
    )
    open_result = await db.execute(open_query)
    open_investigations = open_result.scalar() or 0

    # Count pending reviews (investigations awaiting human review)
    pending_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.phase == "human_review",
        InvestigationReadModel.status == "in_progress",
    )
    pending_result = await db.execute(pending_query)
    pending_reviews = pending_result.scalar() or 0

    # Get today's hourly metrics
    hourly_query = select(MetricsHourly).where(MetricsHourly.hour >= today_start)
    hourly_result = await db.execute(hourly_query)
    hourly_metrics = hourly_result.scalars().all()

    # Aggregate today's metrics
    investigations_created_today = sum(m.investigations_created for m in hourly_metrics)
    investigations_closed_today = sum(m.investigations_closed for m in hourly_metrics)
    escalations_today = sum(m.escalations for m in hourly_metrics)
    auto_closed_today = sum(m.auto_closed for m in hourly_metrics)
    total_alerts_today = sum(m.total_alerts for m in hourly_metrics)
    total_observables_today = sum(m.total_observables for m in hourly_metrics)
    malicious_observables_today = sum(m.malicious_observables for m in hourly_metrics)

    # Calculate average time to verdict from today's data
    verdict_times = [
        m.avg_time_to_verdict_seconds
        for m in hourly_metrics
        if m.avg_time_to_verdict_seconds is not None
    ]
    avg_time_to_verdict = int(sum(verdict_times) / len(verdict_times)) if verdict_times else None

    # Get average time to triage from investigations
    triage_query = select(func.avg(InvestigationReadModel.time_to_triage_seconds)).where(
        InvestigationReadModel.time_to_triage_seconds.isnot(None),
        InvestigationReadModel.created_at >= today_start,
    )
    triage_result = await db.execute(triage_query)
    avg_triage = triage_result.scalar()
    avg_time_to_triage = int(avg_triage) if avg_triage else None

    # Get severity breakdown
    severity_query = select(
        InvestigationReadModel.max_severity,
        func.count().label("count"),
    ).where(
        InvestigationReadModel.status.in_(["pending", "in_progress", "paused"])
    ).group_by(InvestigationReadModel.max_severity)
    severity_result = await db.execute(severity_query)
    severity_breakdown = {row[0] or "unknown": row[1] for row in severity_result.all()}

    # Get verdict breakdown (today)
    verdict_query = select(
        InvestigationReadModel.verdict_decision,
        func.count().label("count"),
    ).where(
        InvestigationReadModel.created_at >= today_start,
        InvestigationReadModel.verdict_decision.isnot(None),
    ).group_by(InvestigationReadModel.verdict_decision)
    verdict_result = await db.execute(verdict_query)
    verdict_breakdown = {row[0]: row[1] for row in verdict_result.all()}

    return MetricsOverview(
        open_investigations=open_investigations,
        pending_reviews=pending_reviews,
        investigations_created_today=investigations_created_today,
        investigations_closed_today=investigations_closed_today,
        escalations_today=escalations_today,
        auto_closed_today=auto_closed_today,
        avg_time_to_triage_seconds=avg_time_to_triage,
        avg_time_to_verdict_seconds=avg_time_to_verdict,
        total_alerts_today=total_alerts_today,
        total_observables_today=total_observables_today,
        malicious_observables_today=malicious_observables_today,
        severity_breakdown=severity_breakdown,
        verdict_breakdown=verdict_breakdown,
    )


@router.get("/metrics/hourly", response_model=HourlyMetricsResponse)
async def get_hourly_metrics(
    db: DbSession,
    hours: int = Query(24, ge=1, le=168, description="Number of hours to retrieve"),
) -> HourlyMetricsResponse:
    """Get hourly metrics for charting.

    Args:
        db: Database session.
        hours: Number of hours to retrieve (max 168 = 1 week).

    Returns:
        Hourly metrics for the specified time range.
    """
    now = datetime.utcnow()
    start = now - timedelta(hours=hours)

    # Get hourly metrics
    query = (
        select(MetricsHourly)
        .where(MetricsHourly.hour >= start)
        .order_by(MetricsHourly.hour)
    )
    result = await db.execute(query)
    metrics = list(result.scalars().all())

    # Get current open investigations count
    open_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.status.in_(["pending", "in_progress", "paused"])
    )
    open_result = await db.execute(open_query)
    current_open = open_result.scalar() or 0

    # Compute open_wip for each hour by working backwards from current state
    # open_wip[i] = open count at end of hour i
    open_wip_values: list[int] = []
    running_open = current_open

    # Process in reverse order (newest to oldest)
    for m in reversed(metrics):
        # This is the open count at end of this hour
        open_wip_values.insert(0, running_open)
        # Going back in time: undo net change of this hour
        # created added to queue, closed/escalated removed from queue
        running_open = max(
            0,
            running_open
            - m.investigations_created
            + m.investigations_closed
            + m.escalations,
        )

    return HourlyMetricsResponse(
        metrics=[
            HourlyMetric(
                hour=m.hour,
                investigations_created=m.investigations_created,
                investigations_closed=m.investigations_closed,
                escalations=m.escalations,
                auto_closed=m.auto_closed,
                avg_time_to_verdict_seconds=m.avg_time_to_verdict_seconds,
                total_alerts=m.total_alerts,
                total_observables=m.total_observables,
                malicious_observables=m.malicious_observables,
                open_wip=open_wip_values[i] if i < len(open_wip_values) else 0,
            )
            for i, m in enumerate(metrics)
        ],
        start=start,
        end=now,
        total_hours=len(metrics),
    )


@router.get("/stats/iocs", response_model=IOCStatsResponse)
async def get_ioc_stats(
    db: DbSession,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=100, description="Items per page"),
    type: str | None = Query(None, description="Filter by IOC type"),
    malicious_only: bool = Query(False, description="Only show malicious IOCs"),
    sort_by: str = Query(
        "times_seen", description="Sort by: times_seen, malicious_count, last_seen"
    ),
) -> IOCStatsResponse:
    """Get IOC statistics.

    Args:
        db: Database session.
        page: Page number (1-indexed).
        page_size: Number of items per page.
        type: Filter by IOC type (ip, domain, hash, url, etc.).
        malicious_only: Only show IOCs with malicious verdicts.
        sort_by: Sort field.

    Returns:
        Paginated IOC statistics.
    """
    # Build query
    conditions = []
    if type:
        conditions.append(IOCStats.type == type)
    if malicious_only:
        conditions.append(IOCStats.malicious_count > 0)

    # Count total
    count_query = select(func.count()).select_from(IOCStats)
    if conditions:
        count_query = count_query.where(*conditions)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Build order by
    order_column = {
        "times_seen": desc(IOCStats.times_seen),
        "malicious_count": desc(IOCStats.malicious_count),
        "last_seen": desc(IOCStats.last_seen),
    }.get(sort_by, desc(IOCStats.times_seen))

    # Fetch page
    offset = (page - 1) * page_size
    query = select(IOCStats).order_by(order_column).offset(offset).limit(page_size)
    if conditions:
        query = query.where(*conditions)

    result = await db.execute(query)
    items = result.scalars().all()

    return IOCStatsResponse(
        items=[
            IOCStatItem(
                id=ioc.id,
                value=ioc.value,
                type=ioc.type,
                times_seen=ioc.times_seen,
                last_seen=ioc.last_seen,
                malicious_count=ioc.malicious_count,
                benign_count=ioc.benign_count,
                threat_actors=ioc.threat_actors or [],
                malicious_rate=(
                    ioc.malicious_count / ioc.times_seen
                    if ioc.times_seen > 0
                    else 0.0
                ),
            )
            for ioc in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.get("/stats/rules", response_model=RuleStatsResponse)
async def get_rule_stats(
    db: DbSession,
    limit: int = Query(50, ge=1, le=200, description="Number of rules to return"),
    sort_by: str = Query(
        "times_triggered",
        description="Sort by: times_triggered, escalation_count, precision_rate",
    ),
) -> RuleStatsResponse:
    """Get Wazuh rule statistics.

    Args:
        db: Database session.
        limit: Number of rules to return.
        sort_by: Sort field.

    Returns:
        Rule statistics sorted by the specified field.
    """
    # Build order by
    order_column = {
        "times_triggered": desc(RuleStats.times_triggered),
        "escalation_count": desc(RuleStats.escalation_count),
        "precision_rate": desc(RuleStats.precision_rate),
    }.get(sort_by, desc(RuleStats.times_triggered))

    query = select(RuleStats).order_by(order_column).limit(limit)
    result = await db.execute(query)
    rules = result.scalars().all()

    return RuleStatsResponse(
        items=[
            RuleStatItem(
                rule_id=rule.rule_id,
                times_triggered=rule.times_triggered,
                escalation_count=rule.escalation_count,
                auto_close_count=rule.auto_close_count,
                precision_rate=rule.precision_rate,
                escalation_rate=(
                    rule.escalation_count / rule.times_triggered
                    if rule.times_triggered > 0
                    else 0.0
                ),
            )
            for rule in rules
        ],
        total=len(rules),
    )


@router.get("/stats/analyzers", response_model=AnalyzerStatsResponse)
async def get_analyzer_stats(
    db: DbSession,
    sort_by: str = Query(
        "invocations", description="Sort by: invocations, success_rate, avg_response_time_ms"
    ),
) -> AnalyzerStatsResponse:
    """Get Cortex analyzer statistics.

    Args:
        db: Database session.
        sort_by: Sort field.

    Returns:
        Analyzer statistics.
    """
    # Build order by
    order_column = {
        "invocations": desc(AnalyzerStats.invocations),
        "success_rate": desc(AnalyzerStats.successes),  # Approximation
        "avg_response_time_ms": AnalyzerStats.avg_response_time_ms,  # Ascending for fastest
    }.get(sort_by, desc(AnalyzerStats.invocations))

    query = select(AnalyzerStats).order_by(order_column)
    result = await db.execute(query)
    analyzers = result.scalars().all()

    return AnalyzerStatsResponse(
        items=[
            AnalyzerStatItem(
                analyzer=analyzer.analyzer,
                invocations=analyzer.invocations,
                successes=analyzer.successes,
                failures=analyzer.failures,
                avg_response_time_ms=analyzer.avg_response_time_ms,
                success_rate=(
                    analyzer.successes / analyzer.invocations
                    if analyzer.invocations > 0
                    else 0.0
                ),
            )
            for analyzer in analyzers
        ],
        total=len(analyzers),
    )
