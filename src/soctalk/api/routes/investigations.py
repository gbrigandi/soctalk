"""Investigation CRUD and control endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select

from soctalk.api.auth import UserIdentity, require_analyst
from soctalk.api.deps import DbSession
from soctalk.persistence.events import EventType
from soctalk.persistence.models import Event, InvestigationReadModel
from soctalk.persistence.projector import ProjectingEventStore

logger = structlog.get_logger()

router = APIRouter(prefix="/investigations", tags=["investigations"])


# Response models
class InvestigationSummary(BaseModel):
    """Summary view of an investigation for list responses."""

    id: UUID
    title: str | None
    status: str
    phase: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    alert_count: int
    observable_count: int
    malicious_count: int
    suspicious_count: int
    clean_count: int
    max_severity: str | None
    verdict_decision: str | None
    thehive_case_id: str | None


class InvestigationDetail(InvestigationSummary):
    """Full investigation detail including metrics."""

    time_to_triage_seconds: int | None
    time_to_verdict_seconds: int | None
    verdict_confidence: float | None
    verdict_reasoning: str | None
    threat_actor: str | None
    tags: list[str]


class InvestigationList(BaseModel):
    """Paginated list of investigations."""

    items: list[InvestigationSummary]
    total: int
    page: int
    page_size: int
    has_more: bool


class EventItem(BaseModel):
    """Event in the investigation timeline."""

    id: UUID
    event_type: str
    timestamp: datetime
    data: dict[str, Any]


class EventTimeline(BaseModel):
    """Timeline of events for an investigation."""

    investigation_id: UUID
    events: list[EventItem]
    total: int


class ActionResponse(BaseModel):
    """Response for control actions (pause/resume/cancel)."""

    success: bool
    message: str
    investigation_id: UUID


class CancelRequest(BaseModel):
    """Optional request body for cancellation."""

    reason: str | None = None


@router.get("", response_model=InvestigationList)
async def list_investigations(
    db: DbSession,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    status: str | None = Query(None, description="Filter by status"),
    phase: str | None = Query(None, description="Filter by phase"),
    severity: str | None = Query(None, description="Filter by max severity"),
    verdict: str | None = Query(None, description="Filter by verdict decision"),
    has_case: bool | None = Query(None, description="Filter by TheHive case existence"),
) -> InvestigationList:
    """List investigations with optional filters and pagination.

    Args:
        db: Database session.
        page: Page number (1-indexed).
        page_size: Number of items per page.
        status: Filter by status (pending, in_progress, closed, escalated, etc.).
        phase: Filter by phase (triage, enrichment, verdict, etc.).
        severity: Filter by max severity (low, medium, high, critical).
        verdict: Filter by verdict decision (escalate, close, etc.).
        has_case: Filter by whether a TheHive case was created.

    Returns:
        Paginated list of investigations.
    """
    # Build query with filters
    conditions = []

    if status:
        conditions.append(InvestigationReadModel.status == status)
    if phase:
        conditions.append(InvestigationReadModel.phase == phase)
    if severity:
        conditions.append(InvestigationReadModel.max_severity == severity)
    if verdict:
        conditions.append(InvestigationReadModel.verdict_decision == verdict)
    if has_case is not None:
        if has_case:
            conditions.append(InvestigationReadModel.thehive_case_id.isnot(None))
        else:
            conditions.append(InvestigationReadModel.thehive_case_id.is_(None))

    # Count total
    count_query = select(func.count()).select_from(InvestigationReadModel)
    if conditions:
        count_query = count_query.where(and_(*conditions))

    result = await db.execute(count_query)
    total = result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    query = (
        select(InvestigationReadModel)
        .order_by(desc(InvestigationReadModel.created_at))
        .offset(offset)
        .limit(page_size)
    )
    if conditions:
        query = query.where(and_(*conditions))

    result = await db.execute(query)
    items = result.scalars().all()

    return InvestigationList(
        items=[
            InvestigationSummary(
                id=inv.id,
                title=inv.title,
                status=inv.status,
                phase=inv.phase,
                created_at=inv.created_at,
                updated_at=inv.updated_at,
                closed_at=inv.closed_at,
                alert_count=inv.alert_count,
                observable_count=inv.observable_count,
                malicious_count=inv.malicious_count,
                suspicious_count=inv.suspicious_count,
                clean_count=inv.clean_count,
                max_severity=inv.max_severity,
                verdict_decision=inv.verdict_decision,
                thehive_case_id=inv.thehive_case_id,
            )
            for inv in items
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(items) < total,
    )


@router.get("/{investigation_id}", response_model=InvestigationDetail)
async def get_investigation(
    investigation_id: UUID,
    db: DbSession,
) -> InvestigationDetail:
    """Get detailed information about a specific investigation.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.

    Returns:
        Full investigation details.

    Raises:
        HTTPException: 404 if investigation not found.
    """
    query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    result = await db.execute(query)
    inv = result.scalar_one_or_none()

    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    return InvestigationDetail(
        id=inv.id,
        title=inv.title,
        status=inv.status,
        phase=inv.phase,
        created_at=inv.created_at,
        updated_at=inv.updated_at,
        closed_at=inv.closed_at,
        alert_count=inv.alert_count,
        observable_count=inv.observable_count,
        malicious_count=inv.malicious_count,
        suspicious_count=inv.suspicious_count,
        clean_count=inv.clean_count,
        max_severity=inv.max_severity,
        verdict_decision=inv.verdict_decision,
        thehive_case_id=inv.thehive_case_id,
        time_to_triage_seconds=inv.time_to_triage_seconds,
        time_to_verdict_seconds=inv.time_to_verdict_seconds,
        verdict_confidence=inv.verdict_confidence,
        verdict_reasoning=inv.verdict_reasoning,
        threat_actor=inv.threat_actor,
        tags=inv.tags or [],
    )


@router.get("/{investigation_id}/events", response_model=EventTimeline)
async def get_investigation_events(
    investigation_id: UUID,
    db: DbSession,
    limit: int = Query(100, ge=1, le=500, description="Max events to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> EventTimeline:
    """Get the event timeline for an investigation.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.
        limit: Maximum number of events to return.
        offset: Offset for pagination.

    Returns:
        Timeline of events for the investigation.

    Raises:
        HTTPException: 404 if investigation not found.
    """
    # Verify investigation exists
    inv_query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    result = await db.execute(inv_query)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    # Count total events
    count_query = (
        select(func.count())
        .select_from(Event)
        .where(Event.aggregate_id == investigation_id)
    )
    result = await db.execute(count_query)
    total = result.scalar() or 0

    # Fetch events
    query = (
        select(Event)
        .where(Event.aggregate_id == investigation_id)
        .order_by(Event.timestamp)
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    events = result.scalars().all()

    return EventTimeline(
        investigation_id=investigation_id,
        events=[
            EventItem(
                id=evt.id,
                event_type=evt.event_type,
                timestamp=evt.timestamp,
                data=evt.data,
            )
            for evt in events
        ],
        total=total,
    )


@router.post("/{investigation_id}/pause", response_model=ActionResponse)
async def pause_investigation(
    investigation_id: UUID,
    db: DbSession,
    _: UserIdentity | None = Depends(require_analyst),
) -> ActionResponse:
    """Pause an active investigation.

    This will signal the investigation workflow to pause at the next
    checkpoint. The investigation can be resumed later.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if investigation not found.
        HTTPException: 400 if investigation cannot be paused.
    """
    query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    result = await db.execute(query)
    inv = result.scalar_one_or_none()

    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if inv.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause investigation with status: {inv.status}",
        )

    store = ProjectingEventStore(db)
    await store.append(
        aggregate_id=investigation_id,
        aggregate_type="Investigation",
        event_type=EventType.INVESTIGATION_PAUSED,
        data={},
    )

    logger.info("investigation_paused", investigation_id=str(investigation_id))

    return ActionResponse(
        success=True,
        message="Investigation paused successfully",
        investigation_id=investigation_id,
    )


@router.post("/{investigation_id}/resume", response_model=ActionResponse)
async def resume_investigation(
    investigation_id: UUID,
    db: DbSession,
    _: UserIdentity | None = Depends(require_analyst),
) -> ActionResponse:
    """Resume a paused investigation.

    This will signal the investigation workflow to continue processing
    from where it was paused.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if investigation not found.
        HTTPException: 400 if investigation cannot be resumed.
    """
    query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    result = await db.execute(query)
    inv = result.scalar_one_or_none()

    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if inv.status != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume investigation with status: {inv.status}",
        )

    store = ProjectingEventStore(db)
    await store.append(
        aggregate_id=investigation_id,
        aggregate_type="Investigation",
        event_type=EventType.INVESTIGATION_RESUMED,
        data={},
    )

    logger.info("investigation_resumed", investigation_id=str(investigation_id))

    return ActionResponse(
        success=True,
        message="Investigation resumed successfully",
        investigation_id=investigation_id,
    )


@router.post("/{investigation_id}/cancel", response_model=ActionResponse)
async def cancel_investigation(
    investigation_id: UUID,
    db: DbSession,
    payload: CancelRequest | None = None,
    _: UserIdentity | None = Depends(require_analyst),
) -> ActionResponse:
    """Cancel an investigation.

    This will terminate the investigation workflow. The investigation
    cannot be resumed after cancellation.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.

    Returns:
        Action result.

    Raises:
        HTTPException: 404 if investigation not found.
        HTTPException: 400 if investigation cannot be cancelled.
    """
    query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    result = await db.execute(query)
    inv = result.scalar_one_or_none()

    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if inv.status in ("cancelled", "closed", "auto_closed", "escalated", "rejected"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel investigation with status: {inv.status}",
        )

    data: dict[str, Any] = {}
    if payload and payload.reason:
        data["reason"] = payload.reason

    store = ProjectingEventStore(db)
    await store.append(
        aggregate_id=investigation_id,
        aggregate_type="Investigation",
        event_type=EventType.INVESTIGATION_CANCELLED,
        data=data,
    )

    logger.info("investigation_cancelled", investigation_id=str(investigation_id))

    return ActionResponse(
        success=True,
        message="Investigation cancelled successfully",
        investigation_id=investigation_id,
    )
