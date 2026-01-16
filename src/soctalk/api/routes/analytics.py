"""Analytics endpoints for the SOC dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.api.deps import DbSession
from soctalk.persistence.models import (
    Event,
    InvestigationReadModel,
    PendingReview,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/analytics", tags=["analytics"])


class ExecutiveKPIs(BaseModel):
    """Executive-level KPIs for AI SOC performance."""

    # Core metrics
    auto_close_rate: float = Field(description="% of investigations auto-closed by AI")
    escalation_rate: float = Field(description="% of investigations escalated")
    human_override_rate: float = Field(description="% of AI decisions changed by humans")
    mean_time_to_decision_seconds: int | None = Field(
        description="Average time from alert to decision"
    )

    # Volume
    total_investigations: int
    auto_closed_count: int
    escalated_count: int
    human_reviewed_count: int

    # Confidence
    avg_ai_confidence: float | None
    high_confidence_rate: float = Field(description="% of decisions with >80% confidence")


class ConfidenceBucket(BaseModel):
    """Confidence distribution bucket."""

    range_label: str  # e.g., "0-20%", "20-40%"
    count: int
    percentage: float


class DecisionTrend(BaseModel):
    """Decision counts for a time period."""

    period: str  # ISO date or hour
    close: int = 0
    escalate: int = 0
    needs_more_info: int = 0
    suspicious: int = 0


class EscalationReason(BaseModel):
    """Breakdown of escalation reasons."""

    reason: str
    count: int
    percentage: float


class AIBehavior(BaseModel):
    """AI behavior analytics."""

    confidence_distribution: list[ConfidenceBucket]
    decision_trends: list[DecisionTrend]
    escalation_breakdown: list[EscalationReason]
    avg_confidence_by_decision: dict[str, float]


class HumanReviewStats(BaseModel):
    """Human-in-the-loop statistics."""

    total_reviews: int
    approved: int
    rejected: int
    info_requested: int
    expired: int
    pending: int

    approval_rate: float
    rejection_rate: float
    avg_review_time_seconds: int | None

    # Override analysis
    ai_agreed_count: int  # Human approved AI's recommendation
    ai_overridden_count: int  # Human changed AI's recommendation
    override_rate: float


class OutcomeMetrics(BaseModel):
    """Investigation outcome metrics."""

    # Resolution
    total_closed: int
    avg_resolution_time_seconds: int | None
    p50_resolution_time_seconds: int | None
    p90_resolution_time_seconds: int | None

    # By verdict
    closed_as_false_positive: int
    closed_as_true_positive: int
    closed_as_suspicious: int

    # Quality signals (simplified - would need more tracking for full implementation)
    reopen_rate: float = 0.0  # Placeholder - needs event tracking


class AnalyticsSummary(BaseModel):
    """Complete analytics summary."""

    period_start: datetime
    period_end: datetime
    executive_kpis: ExecutiveKPIs
    ai_behavior: AIBehavior
    human_review: HumanReviewStats
    outcomes: OutcomeMetrics


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/summary", response_model=AnalyticsSummary)
async def get_analytics_summary(
    db: DbSession,
    days: int = Query(7, ge=1, le=90, description="Number of days to analyze"),
) -> AnalyticsSummary:
    """Get comprehensive AI analytics summary.

    Args:
        db: Database session.
        days: Number of days to analyze.

    Returns:
        Complete analytics summary with KPIs, AI behavior, human review, and outcomes.
    """
    now = datetime.utcnow()
    period_start = now - timedelta(days=days)

    # Fetch all data in parallel-ish queries
    executive_kpis = await _compute_executive_kpis(db, period_start, now)
    ai_behavior = await _compute_ai_behavior(db, period_start, now)
    human_review = await _compute_human_review_stats(db, period_start, now)
    outcomes = await _compute_outcomes(db, period_start, now)

    return AnalyticsSummary(
        period_start=period_start,
        period_end=now,
        executive_kpis=executive_kpis,
        ai_behavior=ai_behavior,
        human_review=human_review,
        outcomes=outcomes,
    )


@router.get("/kpis", response_model=ExecutiveKPIs)
async def get_executive_kpis(
    db: DbSession,
    days: int = Query(7, ge=1, le=90),
) -> ExecutiveKPIs:
    """Get executive KPIs only."""
    now = datetime.utcnow()
    period_start = now - timedelta(days=days)
    return await _compute_executive_kpis(db, period_start, now)


@router.get("/ai-behavior", response_model=AIBehavior)
async def get_ai_behavior(
    db: DbSession,
    days: int = Query(7, ge=1, le=90),
) -> AIBehavior:
    """Get AI behavior analytics."""
    now = datetime.utcnow()
    period_start = now - timedelta(days=days)
    return await _compute_ai_behavior(db, period_start, now)


@router.get("/human-review", response_model=HumanReviewStats)
async def get_human_review_stats(
    db: DbSession,
    days: int = Query(7, ge=1, le=90),
) -> HumanReviewStats:
    """Get human-in-the-loop statistics."""
    now = datetime.utcnow()
    period_start = now - timedelta(days=days)
    return await _compute_human_review_stats(db, period_start, now)


@router.get("/outcomes", response_model=OutcomeMetrics)
async def get_outcome_metrics(
    db: DbSession,
    days: int = Query(7, ge=1, le=90),
) -> OutcomeMetrics:
    """Get investigation outcome metrics."""
    now = datetime.utcnow()
    period_start = now - timedelta(days=days)
    return await _compute_outcomes(db, period_start, now)


# ============================================================================
# Helper Functions
# ============================================================================

async def _compute_executive_kpis(
    db: AsyncSession, start: datetime, end: datetime
) -> ExecutiveKPIs:
    """Compute executive KPIs."""

    # Total investigations in period
    total_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
    )
    total_result = await db.execute(total_query)
    total_investigations = total_result.scalar() or 0

    # Auto-close = closed by AI with no human review (AI decision = "close")
    has_review = exists(
        select(1)
        .select_from(PendingReview)
        .where(PendingReview.investigation_id == InvestigationReadModel.id)
    )
    auto_closed_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.closed_at.isnot(None),
        InvestigationReadModel.thehive_case_id.is_(None),
        InvestigationReadModel.verdict_decision == "close",
        ~has_review,
    )
    auto_closed_result = await db.execute(auto_closed_query)
    auto_closed_count = auto_closed_result.scalar() or 0

    # Count escalated by STATUS or TheHive case creation (actual escalations)
    escalated_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        (InvestigationReadModel.status == "escalated") |
        (InvestigationReadModel.thehive_case_id.isnot(None)),
    )
    escalated_result = await db.execute(escalated_query)
    escalated_count = escalated_result.scalar() or 0

    completed_statuses = ["approved", "rejected", "info_requested"]
    human_decision = case(
        (PendingReview.status == "approved", "escalate"),
        (PendingReview.status == "rejected", "close"),
        (PendingReview.status == "info_requested", "needs_more_info"),
        else_=None,
    )

    # Human reviewed count - only completed decisions (exclude pending/expired)
    completed_review_query = select(func.count()).select_from(PendingReview).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
        PendingReview.status.in_(completed_statuses),
        PendingReview.ai_decision.isnot(None),
    )
    completed_review_result = await db.execute(completed_review_query)
    human_reviewed_count = completed_review_result.scalar() or 0

    # Human override count: human decision differs from AI recommendation
    override_query = select(func.count()).select_from(PendingReview).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
        PendingReview.status.in_(completed_statuses),
        PendingReview.ai_decision.isnot(None),
        PendingReview.ai_decision != human_decision,
    )
    override_result = await db.execute(override_query)
    override_count = override_result.scalar() or 0

    confidence_query = select(func.avg(InvestigationReadModel.verdict_confidence)).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.verdict_confidence.isnot(None),
    )
    confidence_result = await db.execute(confidence_query)
    avg_confidence = confidence_result.scalar()

    high_conf_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.verdict_confidence > 0.8,
    )
    high_conf_result = await db.execute(high_conf_query)
    high_conf_count = high_conf_result.scalar() or 0

    total_with_confidence_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.verdict_confidence.isnot(None),
    )
    total_with_confidence_result = await db.execute(total_with_confidence_query)
    total_with_confidence = total_with_confidence_result.scalar() or 0

    responded_subq = (
        select(
            PendingReview.investigation_id.label("investigation_id"),
            func.max(PendingReview.responded_at).label("responded_at"),
        )
        .where(
            PendingReview.responded_at.isnot(None),
            PendingReview.responded_at >= start,
            PendingReview.responded_at <= end,
        )
        .group_by(PendingReview.investigation_id)
        .subquery()
    )

    decision_time_seconds = case(
        (
            responded_subq.c.responded_at.isnot(None),
            func.extract(
                "epoch",
                responded_subq.c.responded_at - InvestigationReadModel.created_at,
            ),
        ),
        (
            InvestigationReadModel.time_to_verdict_seconds.isnot(None),
            InvestigationReadModel.time_to_verdict_seconds,
        ),
        (
            InvestigationReadModel.closed_at.isnot(None),
            func.extract(
                "epoch",
                InvestigationReadModel.closed_at - InvestigationReadModel.created_at,
            ),
        ),
        else_=None,
    )

    time_query = (
        select(func.avg(decision_time_seconds))
        .select_from(InvestigationReadModel)
        .outerjoin(responded_subq, responded_subq.c.investigation_id == InvestigationReadModel.id)
        .where(
            InvestigationReadModel.created_at >= start,
            InvestigationReadModel.created_at <= end,
        )
    )
    time_result = await db.execute(time_query)
    avg_time = time_result.scalar()

    auto_close_rate = (
        auto_closed_count / total_investigations if total_investigations > 0 else 0.0
    )
    escalation_rate = (
        escalated_count / total_investigations if total_investigations > 0 else 0.0
    )
    human_override_rate = (
        override_count / human_reviewed_count if human_reviewed_count > 0 else 0.0
    )
    high_confidence_rate = (
        high_conf_count / total_with_confidence if total_with_confidence > 0 else 0.0
    )

    return ExecutiveKPIs(
        auto_close_rate=auto_close_rate,
        escalation_rate=escalation_rate,
        human_override_rate=human_override_rate,
        mean_time_to_decision_seconds=int(avg_time) if avg_time else None,
        total_investigations=total_investigations,
        auto_closed_count=auto_closed_count,
        escalated_count=escalated_count,
        human_reviewed_count=human_reviewed_count,
        avg_ai_confidence=round(avg_confidence, 3) if avg_confidence else None,
        high_confidence_rate=high_confidence_rate,
    )


async def _compute_ai_behavior(
    db: AsyncSession, start: datetime, end: datetime
) -> AIBehavior:
    """Compute AI behavior analytics."""

    # Confidence distribution from verdict confidence (AI confidence at verdict time)
    buckets = [
        ("0-20%", 0.0, 0.2),
        ("20-40%", 0.2, 0.4),
        ("40-60%", 0.4, 0.6),
        ("60-80%", 0.6, 0.8),
        ("80-100%", 0.8, 1.0),
    ]

    confidence_dist = []
    total_with_confidence = 0

    for label, low, high in buckets:
        query = select(func.count()).select_from(InvestigationReadModel).where(
            InvestigationReadModel.created_at >= start,
            InvestigationReadModel.created_at <= end,
            InvestigationReadModel.verdict_confidence.isnot(None),
            InvestigationReadModel.verdict_confidence >= low,
            (
                InvestigationReadModel.verdict_confidence < high
                if high < 1.0
                else InvestigationReadModel.verdict_confidence <= high
            ),
        )
        result = await db.execute(query)
        count = result.scalar() or 0
        total_with_confidence += count
        confidence_dist.append(ConfidenceBucket(range_label=label, count=count, percentage=0.0))

    # Calculate percentages
    for bucket in confidence_dist:
        bucket.percentage = (
            bucket.count / total_with_confidence if total_with_confidence > 0 else 0.0
        )

    # Decision trends (daily) from verdict.rendered events (source of truth for AI verdicts)
    day = func.date_trunc("day", Event.timestamp)
    decision_expr = Event.data["decision"].as_string()
    trend_query = (
        select(
            day.label("day"),
            decision_expr.label("decision"),
            func.count().label("count"),
        )
        .where(
            Event.timestamp >= start,
            Event.timestamp <= end,
            Event.event_type == "verdict.rendered",
            decision_expr.isnot(None),
        )
        .group_by(day, decision_expr)
        .order_by(day)
    )

    trend_result = await db.execute(trend_query)
    trend_rows = trend_result.all()

    # Group by day
    trends_by_day: dict[str, DecisionTrend] = {}
    for row in trend_rows:
        day_str = row[0].strftime("%Y-%m-%d") if row[0] else "unknown"
        decision = (row[1] or "unknown").lower()
        count = row[2]

        if day_str not in trends_by_day:
            trends_by_day[day_str] = DecisionTrend(period=day_str)

        trend = trends_by_day[day_str]
        if decision == "close":
            trend.close += count
        elif decision == "escalate":
            trend.escalate += count
        elif decision == "needs_more_info":
            trend.needs_more_info += count
        elif decision == "suspicious":
            trend.suspicious += count

    decision_trends = list(trends_by_day.values())

    # Escalation breakdown by severity - use actual escalated investigations
    escalation_query = select(
        InvestigationReadModel.max_severity,
        func.count().label('count'),
    ).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        (InvestigationReadModel.status == "escalated") |
        (InvestigationReadModel.thehive_case_id.isnot(None)),
    ).group_by(InvestigationReadModel.max_severity)

    escalation_result = await db.execute(escalation_query)
    escalation_rows = escalation_result.all()

    total_escalations = sum(row[1] for row in escalation_rows)
    severity_labels = {
        'critical': 'Critical Severity',
        'high': 'High Severity',
        'medium': 'Medium Severity',
        'low': 'Low Severity',
    }
    escalation_breakdown = [
        EscalationReason(
            reason=severity_labels.get(row[0], row[0] or 'Unknown') if row[0] else 'Unknown',
            count=row[1],
            percentage=row[1] / total_escalations if total_escalations > 0 else 0.0,
        )
        for row in escalation_rows
    ]

    # Average AI confidence by AI decision (from verdict.rendered events)
    conf_expr = Event.data["confidence"].as_float()
    conf_by_decision_query = (
        select(
            decision_expr.label("decision"),
            func.avg(conf_expr).label("avg_conf"),
        )
        .where(
            Event.timestamp >= start,
            Event.timestamp <= end,
            Event.event_type == "verdict.rendered",
            decision_expr.isnot(None),
            conf_expr.isnot(None),
        )
        .group_by(decision_expr)
    )

    conf_result = await db.execute(conf_by_decision_query)
    avg_confidence_by_decision = {
        (row[0] or "unknown"): round(row[1], 3) if row[1] is not None else 0.0
        for row in conf_result.all()
        if row[0] is not None
    }

    return AIBehavior(
        confidence_distribution=confidence_dist,
        decision_trends=decision_trends,
        escalation_breakdown=escalation_breakdown,
        avg_confidence_by_decision=avg_confidence_by_decision,
    )


async def _compute_human_review_stats(
    db: AsyncSession, start: datetime, end: datetime
) -> HumanReviewStats:
    """Compute human-in-the-loop statistics."""

    # Count by status
    status_query = select(
        PendingReview.status,
        func.count().label('count'),
    ).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
    ).group_by(PendingReview.status)

    status_result = await db.execute(status_query)
    status_counts = {row[0]: row[1] for row in status_result.all()}

    total_reviews = sum(status_counts.values())
    approved = status_counts.get("approved", 0)
    rejected = status_counts.get("rejected", 0)
    info_requested = status_counts.get("info_requested", 0)
    expired = status_counts.get("expired", 0)
    pending = status_counts.get("pending", 0)

    # Average review time (responded_at - created_at)
    review_time_query = select(
        func.avg(
            func.extract(
                "epoch",
                PendingReview.responded_at - PendingReview.created_at,
            )
        )
    ).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
        PendingReview.responded_at.isnot(None),
    )
    review_time_result = await db.execute(review_time_query)
    avg_review_time = review_time_result.scalar()

    completed_statuses = ["approved", "rejected", "info_requested"]
    human_decision = case(
        (PendingReview.status == "approved", "escalate"),
        (PendingReview.status == "rejected", "close"),
        (PendingReview.status == "info_requested", "needs_more_info"),
        else_=None,
    )

    agreed_query = select(func.count()).select_from(PendingReview).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
        PendingReview.status.in_(completed_statuses),
        PendingReview.ai_decision.isnot(None),
        PendingReview.ai_decision == human_decision,
    )
    agreed_result = await db.execute(agreed_query)
    ai_agreed_count = agreed_result.scalar() or 0

    overridden_query = select(func.count()).select_from(PendingReview).where(
        PendingReview.created_at >= start,
        PendingReview.created_at <= end,
        PendingReview.status.in_(completed_statuses),
        PendingReview.ai_decision.isnot(None),
        PendingReview.ai_decision != human_decision,
    )
    overridden_result = await db.execute(overridden_query)
    ai_overridden_count = overridden_result.scalar() or 0

    completed_reviews = approved + rejected + info_requested
    decided_with_ai = ai_agreed_count + ai_overridden_count

    return HumanReviewStats(
        total_reviews=total_reviews,
        approved=approved,
        rejected=rejected,
        info_requested=info_requested,
        expired=expired,
        pending=pending,
        approval_rate=approved / completed_reviews if completed_reviews > 0 else 0.0,
        rejection_rate=rejected / completed_reviews if completed_reviews > 0 else 0.0,
        avg_review_time_seconds=int(avg_review_time) if avg_review_time else None,
        ai_agreed_count=ai_agreed_count,
        ai_overridden_count=ai_overridden_count,
        override_rate=ai_overridden_count / decided_with_ai if decided_with_ai > 0 else 0.0,
    )


async def _compute_outcomes(
    db: AsyncSession, start: datetime, end: datetime
) -> OutcomeMetrics:
    """Compute investigation outcome metrics."""

    # Total closed (any terminal state)
    closed_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.closed_at.isnot(None),
    )
    closed_result = await db.execute(closed_query)
    total_closed = closed_result.scalar() or 0

    # By status - auto_closed = false positive, escalated = true positive
    status_query = select(
        InvestigationReadModel.status,
        func.count().label('count'),
    ).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.closed_at.isnot(None),
    ).group_by(InvestigationReadModel.status)

    status_result = await db.execute(status_query)
    status_counts = {row[0]: row[1] for row in status_result.all()}

    # auto_closed and closed (rejected) = false positive
    closed_as_fp = (
        status_counts.get("auto_closed", 0)
        + status_counts.get("closed", 0)
        + status_counts.get("rejected", 0)
    )
    # escalated = true positive (incident created)
    closed_as_tp = status_counts.get("escalated", 0)
    # Any status with suspicious verdict
    suspicious_query = select(func.count()).select_from(InvestigationReadModel).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.closed_at.isnot(None),
        InvestigationReadModel.verdict_decision == "suspicious",
    )
    suspicious_result = await db.execute(suspicious_query)
    closed_as_suspicious = suspicious_result.scalar() or 0

    # Resolution time stats - use actual resolution time (closed_at - created_at)
    time_query = select(
        func.avg(
            func.extract(
                "epoch",
                InvestigationReadModel.closed_at - InvestigationReadModel.created_at,
            )
        ),
    ).where(
        InvestigationReadModel.created_at >= start,
        InvestigationReadModel.created_at <= end,
        InvestigationReadModel.closed_at.isnot(None),
    )

    try:
        time_result = await db.execute(time_query)
        avg_time = time_result.scalar()

        # For percentiles, we need to query all values and compute in Python
        # since percentile_cont may not work on all DBs
        all_times_query = select(
            func.extract(
                "epoch",
                InvestigationReadModel.closed_at - InvestigationReadModel.created_at,
            )
        ).where(
            InvestigationReadModel.created_at >= start,
            InvestigationReadModel.created_at <= end,
            InvestigationReadModel.closed_at.isnot(None),
        )
        all_times_result = await db.execute(all_times_query)
        all_times = sorted([row[0] for row in all_times_result.all() if row[0] is not None])

        if all_times:
            p50_idx = int(len(all_times) * 0.5)
            p90_idx = int(len(all_times) * 0.9)
            p50_time = all_times[min(p50_idx, len(all_times) - 1)]
            p90_time = all_times[min(p90_idx, len(all_times) - 1)]
        else:
            p50_time = None
            p90_time = None
    except Exception:
        avg_time = None
        p50_time = None
        p90_time = None

    return OutcomeMetrics(
        total_closed=total_closed,
        avg_resolution_time_seconds=int(avg_time) if avg_time else None,
        p50_resolution_time_seconds=int(p50_time) if p50_time else None,
        p90_resolution_time_seconds=int(p90_time) if p90_time else None,
        closed_as_false_positive=closed_as_fp,
        closed_as_true_positive=closed_as_tp,
        closed_as_suspicious=closed_as_suspicious,
        reopen_rate=0.0,  # Would need event tracking for reopens
    )
