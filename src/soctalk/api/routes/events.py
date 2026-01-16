"""SSE events endpoint for real-time investigation updates."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from uuid import uuid4

import structlog
from fastapi import APIRouter
from sqlalchemy import desc, select
from sse_starlette.sse import EventSourceResponse

from soctalk.api.event_bus import get_event_bus
from soctalk.persistence.database import get_async_session
from soctalk.persistence.models import Event

logger = structlog.get_logger()

router = APIRouter(prefix="/events", tags=["events"])


async def _get_recent_events(limit: int = 20) -> list[dict]:
    """Fetch recent events from database for initial SSE payload.

    Args:
        limit: Maximum number of events to fetch.

    Returns:
        List of event dictionaries, oldest first for chronological display.
    """
    if not os.getenv("DATABASE_URL"):
        return []

    try:
        async with get_async_session() as session:
            # Get events from last hour
            since = datetime.utcnow() - timedelta(hours=1)
            result = await session.execute(
                select(Event)
                .where(Event.timestamp > since)
                .order_by(desc(Event.timestamp))
                .limit(limit)
            )
            events = result.scalars().all()

            # Return in chronological order (oldest first)
            return [
                {
                    "event_type": e.event_type,
                    "id": str(e.id),
                    "timestamp": e.timestamp.isoformat(),
                    **e.data,
                }
                for e in reversed(events)
            ]
    except Exception:
        logger.exception("failed_to_fetch_recent_events")
        return []


async def event_generator() -> AsyncGenerator[dict[str, str], None]:
    """Generate SSE events from the event bus.

    Yields:
        Dictionaries with event, id, and data keys for SSE.
    """
    event_bus = get_event_bus()
    loop = asyncio.get_running_loop()
    async with event_bus.subscription() as (subscriber_id, queue):
        logger.info("sse_client_connecting", subscriber_id=subscriber_id)

        try:
            # Send initial ping to confirm connection
            yield {
                "event": "ping",
                "id": f"ping-{subscriber_id}",
                "data": '{"type": "ping", "message": "connected"}',
            }

            # Send recent events from database so client has initial data
            recent_events = await _get_recent_events()
            logger.info(
                "sending_recent_events",
                count=len(recent_events),
                subscriber_id=subscriber_id,
            )
            for event_data in recent_events:
                yield {
                    "event": "message",
                    "id": event_data["id"],
                    "data": json.dumps(event_data),
                }

            while True:
                try:
                    # Wait for event with timeout for heartbeat
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    # Use "message" event type so frontend onmessage receives it
                    # Include event_type in data so frontend can distinguish event types
                    data_with_type = {
                        "event_type": event.event_type,
                        "id": event.id,
                        "timestamp": event.timestamp.isoformat(),
                        **event.data,
                    }
                    yield {
                        "event": "message",
                        "id": event.id,
                        "data": json.dumps(data_with_type),
                    }
                except TimeoutError:
                    # Send heartbeat ping to keep connection alive
                    yield {
                        "event": "ping",
                        "id": f"ping-{int(loop.time() * 1000)}-{uuid4()}",
                        "data": '{"type": "ping"}',
                    }
        finally:
            logger.info("sse_client_disconnected", subscriber_id=subscriber_id)


@router.get("/stream")
async def stream_events() -> EventSourceResponse:
    """Stream real-time events via Server-Sent Events.

    This endpoint provides a continuous stream of investigation events
    including:
    - investigation.created
    - investigation.closed
    - enrichment.completed
    - thehive.case_created

    Returns:
        EventSourceResponse for SSE streaming.
    """
    return EventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/health")
async def events_health() -> dict[str, str | int]:
    """Check the health of the event streaming system.

    Returns:
        Health status including subscriber count.
    """
    event_bus = get_event_bus()
    return {
        "status": "healthy",
        "subscriber_count": event_bus.subscriber_count,
    }
