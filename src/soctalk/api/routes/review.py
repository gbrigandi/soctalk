"""Human-in-the-loop review endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, select

from soctalk.api.auth import UserIdentity, require_authenticated
from soctalk.api.deps import DbSession
from soctalk.persistence.events import EventType
from soctalk.persistence.models import InvestigationReadModel, PendingReview
from soctalk.persistence.projector import ProjectingEventStore

logger = structlog.get_logger()

router = APIRouter(prefix="/review", tags=["review"])


# Response models
class EnrichmentSummaryResponse(BaseModel):
    """Enrichment result summary."""

    observable_value: str
    observable_type: str
    analyzer: str
    verdict: str
    confidence: float
    details: dict[str, Any] = Field(default_factory=dict)


class MISPContextResponse(BaseModel):
    """MISP threat intelligence context."""

    iocs_checked: int = 0
    iocs_matched: int = 0
    threat_actors: list[str] = Field(default_factory=list)
    campaigns: list[str] = Field(default_factory=list)
    warninglist_hits: int = 0
    matched_events: list[str] = Field(default_factory=list)


class ReviewSummary(BaseModel):
    """Summary view of a pending review for list responses."""

    id: UUID
    investigation_id: UUID
    status: str
    title: str
    description: str
    max_severity: str
    alert_count: int
    malicious_count: int
    suspicious_count: int
    clean_count: int
    findings: list[str]
    enrichments: dict[str, Any]
    misp_context: dict[str, Any] | None
    ai_decision: str | None
    ai_confidence: float | None
    ai_assessment: str | None
    ai_recommendation: str | None
    created_at: datetime
    expires_at: datetime | None


class ReviewDetail(ReviewSummary):
    """Full review detail including enrichments and AI assessment."""

    description: str
    findings: list[str]
    enrichments: list[EnrichmentSummaryResponse]
    misp_context: MISPContextResponse | None
    ai_assessment: str | None
    ai_recommendation: str | None
    timeout_seconds: int
    responded_at: datetime | None
    reviewer: str | None
    feedback: str | None


class ReviewList(BaseModel):
    """Paginated list of pending reviews."""

    items: list[ReviewSummary]
    total: int
    page: int
    page_size: int
    has_more: bool


class ReviewDecisionRequest(BaseModel):
    """Request body for review decisions."""

    reviewer: str | None = Field(None, description="Name or ID of the reviewer")
    feedback: str | None = Field(None, description="Optional feedback or notes")


class ReviewInfoRequest(BaseModel):
    """Request body for requesting additional information."""

    reviewer: str | None = Field(None, description="Name or ID of the reviewer")
    questions: list[str] = Field(..., description="Questions or info needed")


class ReviewActionResponse(BaseModel):
    """Response for review actions."""

    success: bool
    message: str
    review_id: UUID
    investigation_id: UUID
    decision: str


@router.get("/pending", response_model=ReviewList)
async def list_pending_reviews(
    db: DbSession,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    severity: str | None = Query(None, description="Filter by max severity"),
    include_expired: bool = Query(False, description="Include expired reviews"),
) -> ReviewList:
    """List pending human reviews.

    Args:
        db: Database session.
        page: Page number (1-indexed).
        page_size: Number of items per page.
        severity: Filter by max severity (low, medium, high, critical).
        include_expired: Whether to include expired reviews.

    Returns:
        Paginated list of pending reviews.
    """
    conditions = [PendingReview.status == "pending"]

    if severity:
        conditions.append(PendingReview.max_severity == severity)

    if not include_expired:
        # Exclude reviews where expires_at is in the past
        conditions.append(
            (PendingReview.expires_at.is_(None)) | (PendingReview.expires_at > datetime.utcnow())
        )

    # Count total
    count_query = select(func.count()).select_from(PendingReview).where(and_(*conditions))
    result = await db.execute(count_query)
    total = result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    query = (
        select(PendingReview)
        .where(and_(*conditions))
        .order_by(desc(PendingReview.created_at))
        .offset(offset)
        .limit(page_size)
    )

    result = await db.execute(query)
    items = result.scalars().all()

    return ReviewList(
        items=[
            ReviewSummary(
                id=review.id,
                investigation_id=review.investigation_id,
                status=review.status,
                title=review.title,
                description=review.description or "",
                max_severity=review.max_severity,
                alert_count=review.alert_count,
                malicious_count=review.malicious_count,
                suspicious_count=review.suspicious_count,
                clean_count=review.clean_count,
                findings=review.findings or [],
                enrichments=review.enrichments or {},
                misp_context=review.misp_context,
                ai_decision=review.ai_decision,
                ai_confidence=review.ai_confidence,
                ai_assessment=review.ai_assessment,
                ai_recommendation=review.ai_recommendation,
                created_at=review.created_at,
                expires_at=review.expires_at,
            )
            for review in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(
    review_id: UUID,
    db: DbSession,
) -> ReviewDetail:
    """Get detailed information about a specific review.

    Args:
        review_id: UUID of the review.
        db: Database session.

    Returns:
        Full review details.

    Raises:
        HTTPException: 404 if review not found.
    """
    query = select(PendingReview).where(PendingReview.id == review_id)
    result = await db.execute(query)
    review = result.scalar_one_or_none()

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    # Parse enrichments from JSON
    enrichments = []
    if review.enrichments:
        for enr in review.enrichments.get("items", []):
            enrichments.append(
                EnrichmentSummaryResponse(
                    observable_value=enr.get("observable_value", ""),
                    observable_type=enr.get("observable_type", ""),
                    analyzer=enr.get("analyzer", ""),
                    verdict=enr.get("verdict", "unknown"),
                    confidence=enr.get("confidence", 0.0),
                    details=enr.get("details", {}),
                )
            )

    # Parse MISP context
    misp_context = None
    if review.misp_context:
        misp_context = MISPContextResponse(
            iocs_checked=review.misp_context.get("iocs_checked", 0),
            iocs_matched=review.misp_context.get("iocs_matched", 0),
            threat_actors=review.misp_context.get("threat_actors", []),
            campaigns=review.misp_context.get("campaigns", []),
            warninglist_hits=review.misp_context.get("warninglist_hits", 0),
            matched_events=review.misp_context.get("matched_events", []),
        )

    return ReviewDetail(
        id=review.id,
        investigation_id=review.investigation_id,
        status=review.status,
        title=review.title,
        max_severity=review.max_severity,
        alert_count=review.alert_count,
        malicious_count=review.malicious_count,
        suspicious_count=review.suspicious_count,
        clean_count=review.clean_count,
        ai_decision=review.ai_decision,
        ai_confidence=review.ai_confidence,
        created_at=review.created_at,
        expires_at=review.expires_at,
        description=review.description,
        findings=review.findings or [],
        enrichments=enrichments,
        misp_context=misp_context,
        ai_assessment=review.ai_assessment,
        ai_recommendation=review.ai_recommendation,
        timeout_seconds=review.timeout_seconds,
        responded_at=review.responded_at,
        reviewer=review.reviewer,
        feedback=review.feedback,
    )


@router.post("/{review_id}/approve", response_model=ReviewActionResponse)
async def approve_review(
    review_id: UUID,
    request: ReviewDecisionRequest,
    db: DbSession,
    user: UserIdentity | None = Depends(require_authenticated),
) -> ReviewActionResponse:
    """Approve a pending review (escalate the investigation).

    Args:
        review_id: UUID of the review.
        request: Reviewer information and optional feedback.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if review not found.
        HTTPException: 400 if review is not in pending state.
        HTTPException: 409 if review was already decided (race condition).
    """
    # Use FOR UPDATE to lock the row and prevent race conditions
    query = select(PendingReview).where(PendingReview.id == review_id).with_for_update()
    result = await db.execute(query)
    review = result.scalar_one_or_none()

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    if review.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Review already decided via another channel (status: {review.status})",
        )

    reviewer = request.reviewer or (user.username if user else None)

    store = ProjectingEventStore(db)
    await store.append(
        aggregate_id=review.investigation_id,
        aggregate_type="Investigation",
        event_type=EventType.HUMAN_DECISION_RECEIVED,
        data={
            "decision": "approve",
            "feedback": request.feedback,
            "reviewer": reviewer,
        },
    )

    # Update investigation status
    inv_query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == review.investigation_id
    )
    inv_result = await db.execute(inv_query)
    investigation = inv_result.scalar_one_or_none()
    if investigation:
        investigation.status = "in_progress"
        investigation.phase = "escalation"
        investigation.verdict_decision = "escalate"
        investigation.updated_at = datetime.utcnow()

    logger.info(
        "review_approved",
        review_id=str(review_id),
        investigation_id=str(review.investigation_id),
        reviewer=reviewer,
    )

    return ReviewActionResponse(
        success=True,
        message="Review approved - investigation will be escalated",
        review_id=review_id,
        investigation_id=review.investigation_id,
        decision="approved",
    )


@router.post("/{review_id}/reject", response_model=ReviewActionResponse)
async def reject_review(
    review_id: UUID,
    request: ReviewDecisionRequest,
    db: DbSession,
    user: UserIdentity | None = Depends(require_authenticated),
) -> ReviewActionResponse:
    """Reject a pending review (close the investigation).

    Args:
        review_id: UUID of the review.
        request: Reviewer information and optional feedback.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if review not found.
        HTTPException: 400 if review is not in pending state.
        HTTPException: 409 if review was already decided (race condition).
    """
    # Use FOR UPDATE to lock the row and prevent race conditions
    query = select(PendingReview).where(PendingReview.id == review_id).with_for_update()
    result = await db.execute(query)
    review = result.scalar_one_or_none()

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    if review.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Review already decided via another channel (status: {review.status})",
        )

    reviewer = request.reviewer or (user.username if user else None)

    inv_query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == review.investigation_id
    )
    inv_result = await db.execute(inv_query)
    investigation = inv_result.scalar_one_or_none()
    duration_seconds = 0
    if investigation and investigation.created_at:
        duration_seconds = int((datetime.utcnow() - investigation.created_at).total_seconds())

    store = ProjectingEventStore(db)
    await store.append_batch(
        aggregate_id=review.investigation_id,
        aggregate_type="Investigation",
        events=[
            (
                EventType.HUMAN_DECISION_RECEIVED,
                {
                    "decision": "reject",
                    "feedback": request.feedback,
                    "reviewer": reviewer,
                },
                None,
            ),
            (
                EventType.INVESTIGATION_CLOSED,
                {
                    "status": "closed",
                    "resolution": "closed by human review",
                    "verdict_decision": "close",
                    "thehive_case_id": None,
                    "duration_seconds": duration_seconds,
                },
                None,
            ),
        ],
    )

    logger.info(
        "review_rejected",
        review_id=str(review_id),
        investigation_id=str(review.investigation_id),
        reviewer=reviewer,
    )

    return ReviewActionResponse(
        success=True,
        message="Review rejected - investigation will be closed",
        review_id=review_id,
        investigation_id=review.investigation_id,
        decision="rejected",
    )


@router.post("/{review_id}/request-info", response_model=ReviewActionResponse)
async def request_more_info(
    review_id: UUID,
    request: ReviewInfoRequest,
    db: DbSession,
    user: UserIdentity | None = Depends(require_authenticated),
) -> ReviewActionResponse:
    """Request additional information before making a decision.

    This will mark the review as "info_requested" and record
    the questions for follow-up.

    Args:
        review_id: UUID of the review.
        request: Reviewer information and questions.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if review not found.
        HTTPException: 400 if review is not in pending state.
        HTTPException: 409 if review was already decided (race condition).
    """
    # Use FOR UPDATE to lock the row and prevent race conditions
    query = select(PendingReview).where(PendingReview.id == review_id).with_for_update()
    result = await db.execute(query)
    review = result.scalar_one_or_none()

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    if review.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Review already decided via another channel (status: {review.status})",
        )

    reviewer = request.reviewer or (user.username if user else None)
    feedback = "Additional information requested:\n" + "\n".join(
        f"- {q}" for q in request.questions
    )
    store = ProjectingEventStore(db)
    await store.append(
        aggregate_id=review.investigation_id,
        aggregate_type="Investigation",
        event_type=EventType.HUMAN_DECISION_RECEIVED,
        data={
            "decision": "more_info",
            "feedback": feedback,
            "reviewer": reviewer,
        },
    )

    logger.info(
        "review_info_requested",
        review_id=str(review_id),
        investigation_id=str(review.investigation_id),
        reviewer=reviewer,
        questions=request.questions,
    )

    return ReviewActionResponse(
        success=True,
        message="Additional information requested",
        review_id=review_id,
        investigation_id=review.investigation_id,
        decision="info_requested",
    )
