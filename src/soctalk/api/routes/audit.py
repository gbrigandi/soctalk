"""Audit log endpoints for event history."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, select

from soctalk.api.deps import DbSession
from soctalk.persistence.models import Event, InvestigationReadModel

logger = structlog.get_logger()

router = APIRouter(prefix="/audit", tags=["audit"])


# Response models
class AuditEventItem(BaseModel):
    """Single audit event."""

    id: UUID
    aggregate_id: UUID
    aggregate_type: str
    event_type: str
    version: int
    timestamp: datetime
    data: dict[str, Any]
    metadata: dict[str, Any]


class AuditEventList(BaseModel):
    """Paginated list of audit events."""

    items: list[AuditEventItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class InvestigationAuditSummary(BaseModel):
    """Summary of an investigation with its audit trail."""

    investigation_id: UUID
    title: str | None
    status: str
    phase: str
    created_at: datetime
    events: list[AuditEventItem]
    total_events: int


@router.get("", response_model=AuditEventList)
async def list_audit_events(
    db: DbSession,
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    event_type: str | None = Query(None, description="Filter by event type"),
    aggregate_type: str | None = Query(None, description="Filter by aggregate type"),
    start_date: datetime | None = Query(None, description="Filter events after this date"),
    end_date: datetime | None = Query(None, description="Filter events before this date"),
    investigation_id: UUID | None = Query(None, description="Filter by investigation ID"),
) -> AuditEventList:
    """List audit events with optional filters.

    Args:
        db: Database session.
        page: Page number (1-indexed).
        page_size: Number of items per page.
        event_type: Filter by event type (e.g., 'investigation.created').
        aggregate_type: Filter by aggregate type (e.g., 'Investigation').
        start_date: Filter events after this date.
        end_date: Filter events before this date.
        investigation_id: Filter by specific investigation.

    Returns:
        Paginated list of audit events.
    """
    conditions = []

    if event_type:
        conditions.append(Event.event_type == event_type)
    if aggregate_type:
        conditions.append(Event.aggregate_type == aggregate_type)
    if start_date:
        conditions.append(Event.timestamp >= start_date)
    if end_date:
        conditions.append(Event.timestamp <= end_date)
    if investigation_id:
        conditions.append(Event.aggregate_id == investigation_id)

    # Count total
    count_query = select(func.count()).select_from(Event)
    if conditions:
        count_query = count_query.where(and_(*conditions))
    result = await db.execute(count_query)
    total = result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    query = (
        select(Event)
        .order_by(desc(Event.timestamp))
        .offset(offset)
        .limit(page_size)
    )
    if conditions:
        query = query.where(and_(*conditions))

    result = await db.execute(query)
    events = result.scalars().all()

    return AuditEventList(
        items=[
            AuditEventItem(
                id=evt.id,
                aggregate_id=evt.aggregate_id,
                aggregate_type=evt.aggregate_type,
                event_type=evt.event_type,
                version=evt.version,
                timestamp=evt.timestamp,
                data=evt.data,
                metadata=evt.event_metadata,
            )
            for evt in events
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_more=offset + len(events) < total,
    )


@router.get("/investigation/{investigation_id}", response_model=InvestigationAuditSummary)
async def get_investigation_audit(
    investigation_id: UUID,
    db: DbSession,
    limit: int = Query(100, ge=1, le=500, description="Max events to return"),
) -> InvestigationAuditSummary:
    """Get complete audit trail for an investigation.

    Args:
        investigation_id: UUID of the investigation.
        db: Database session.
        limit: Maximum number of events to return.

    Returns:
        Investigation summary with full audit trail.

    Raises:
        HTTPException: 404 if investigation not found.
    """
    # Get investigation
    inv_query = select(InvestigationReadModel).where(
        InvestigationReadModel.id == investigation_id
    )
    inv_result = await db.execute(inv_query)
    investigation = inv_result.scalar_one_or_none()

    if investigation is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    # Count total events
    count_query = (
        select(func.count())
        .select_from(Event)
        .where(Event.aggregate_id == investigation_id)
    )
    count_result = await db.execute(count_query)
    total_events = count_result.scalar() or 0

    # Get events
    events_query = (
        select(Event)
        .where(Event.aggregate_id == investigation_id)
        .order_by(Event.timestamp)
        .limit(limit)
    )
    events_result = await db.execute(events_query)
    events = events_result.scalars().all()

    return InvestigationAuditSummary(
        investigation_id=investigation_id,
        title=investigation.title,
        status=investigation.status,
        phase=investigation.phase,
        created_at=investigation.created_at,
        events=[
            AuditEventItem(
                id=evt.id,
                aggregate_id=evt.aggregate_id,
                aggregate_type=evt.aggregate_type,
                event_type=evt.event_type,
                version=evt.version,
                timestamp=evt.timestamp,
                data=evt.data,
                metadata=evt.event_metadata,
            )
            for evt in events
        ],
        total_events=total_events,
    )


@router.get("/event-types")
async def list_event_types(
    db: DbSession,
) -> dict[str, list[str]]:
    """List all distinct event types in the system.

    Useful for populating filter dropdowns.

    Args:
        db: Database session.

    Returns:
        Dictionary with list of event types.
    """
    query = select(Event.event_type).distinct()
    result = await db.execute(query)
    event_types = [row[0] for row in result.all()]

    return {"event_types": sorted(event_types)}


@router.get("/stats")
async def get_audit_stats(
    db: DbSession,
    hours: int = Query(24, ge=1, le=168, description="Hours to analyze"),
) -> dict[str, Any]:
    """Get audit statistics for the specified time period.

    Args:
        db: Database session.
        hours: Number of hours to analyze.

    Returns:
        Audit statistics including event counts by type.
    """
    now = datetime.utcnow()
    start = now - timedelta(hours=hours)

    # Get all events in period
    query = select(Event).where(Event.timestamp >= start)
    result = await db.execute(query)
    events = result.scalars().all()

    # Count by type
    type_counts: dict[str, int] = {}
    for evt in events:
        type_counts[evt.event_type] = type_counts.get(evt.event_type, 0) + 1

    # Count by hour
    hourly_counts: dict[str, int] = {}
    for evt in events:
        hour_key = evt.timestamp.strftime("%Y-%m-%d %H:00")
        hourly_counts[hour_key] = hourly_counts.get(hour_key, 0) + 1

    # Count unique investigations
    investigation_ids = set(evt.aggregate_id for evt in events)

    return {
        "period_hours": hours,
        "total_events": len(events),
        "unique_investigations": len(investigation_ids),
        "events_by_type": type_counts,
        "events_by_hour": hourly_counts,
    }
