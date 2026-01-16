"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, desc

from soctalk.api.event_bus import get_event_bus, reset_event_bus
from soctalk.api.auth import require_authenticated, require_analyst
from soctalk.api.routes import (
    analytics_router,
    auth_router,
    audit_router,
    events_router,
    investigations_router,
    metrics_router,
    review_router,
    settings_router,
)
from soctalk.persistence.database import close_db, get_async_session, init_db
from soctalk.persistence.models import Event
from soctalk.settings_provider import is_settings_readonly, seed_settings_from_env

logger = structlog.get_logger()

# Background task for event polling
_event_poller_task: asyncio.Task | None = None


async def poll_events_from_database() -> None:
    """Poll the database for new events and broadcast them to SSE clients.

    This bridges the gap between the orchestrator (which stores events in DB)
    and the API's in-memory event bus (which SSE clients subscribe to).
    """
    event_bus = get_event_bus()
    last_timestamp: datetime | None = None
    last_seen_ids: set[str] = set()  # Track seen IDs to avoid duplicates
    poll_interval = 1.0  # Poll every second

    logger.info("event_poller_started")

    # Start by getting the most recent event timestamp (don't broadcast old events)
    try:
        async with get_async_session() as session:
            result = await session.execute(
                select(Event).order_by(desc(Event.timestamp)).limit(1)
            )
            latest = result.scalar_one_or_none()
            if latest:
                last_timestamp = latest.timestamp
                last_seen_ids.add(str(latest.id))
                logger.info("event_poller_initialized", last_timestamp=last_timestamp.isoformat())
    except Exception as e:
        logger.warning("event_poller_init_failed", error=str(e))

    while True:
        try:
            await asyncio.sleep(poll_interval)

            async with get_async_session() as session:
                # Query for events newer than last_timestamp
                if last_timestamp:
                    # Get events from slightly before last_timestamp to catch any with same timestamp
                    query = select(Event).where(
                        Event.timestamp >= last_timestamp - timedelta(seconds=1)
                    ).order_by(Event.timestamp).limit(100)
                else:
                    # First run - get events from last minute
                    since = datetime.utcnow() - timedelta(minutes=1)
                    query = select(Event).where(Event.timestamp > since).order_by(Event.timestamp).limit(100)

                result = await session.execute(query)
                new_events = result.scalars().all()

                broadcast_count = 0
                for event in new_events:
                    event_id_str = str(event.id)
                    # Skip if we've already seen this event
                    if event_id_str in last_seen_ids:
                        continue

                    # Broadcast to SSE clients
                    await event_bus.publish(
                        event_type=event.event_type,
                        data={
                            "id": event_id_str,
                            "aggregate_id": str(event.aggregate_id),
                            "timestamp": event.timestamp.isoformat(),
                            **event.data,
                        },
                        event_id=event_id_str,
                    )
                    last_seen_ids.add(event_id_str)
                    last_timestamp = event.timestamp
                    broadcast_count += 1

                # Keep last_seen_ids from growing too large
                if len(last_seen_ids) > 1000:
                    last_seen_ids = set(list(last_seen_ids)[-500:])

                if broadcast_count > 0:
                    logger.debug(
                        "events_broadcast",
                        count=broadcast_count,
                        last_timestamp=last_timestamp.isoformat() if last_timestamp else None,
                    )

        except asyncio.CancelledError:
            logger.info("event_poller_cancelled")
            break
        except Exception as e:
            logger.warning("event_poller_error", error=str(e))
            await asyncio.sleep(5)  # Back off on errors


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application lifespan - startup and shutdown.

    On startup:
    - Initialize database connection and create tables
    - Start event poller for SSE broadcasting

    On shutdown:
    - Stop event poller
    - Close database connections
    - Reset event bus
    """
    global _event_poller_task

    # Startup
    logger.info("api_starting")

    # Initialize database if DATABASE_URL is configured
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            await init_db()
            logger.info("database_initialized")

            async with get_async_session() as session:
                readonly = is_settings_readonly()
                await seed_settings_from_env(session, overwrite=readonly)
            logger.info("settings_seeded_from_env", readonly=readonly)

            # Start event poller for SSE broadcasting
            _event_poller_task = asyncio.create_task(poll_events_from_database())
            logger.info("event_poller_task_created")
        except Exception as e:
            logger.warning("database_init_failed", error=str(e))
    else:
        logger.info("database_not_configured", msg="Running without persistence")

    yield

    # Shutdown
    logger.info("api_shutting_down")

    # Stop event poller
    if _event_poller_task:
        _event_poller_task.cancel()
        try:
            await _event_poller_task
        except asyncio.CancelledError:
            pass
        logger.info("event_poller_stopped")

    # Close database
    try:
        await close_db()
        logger.info("database_closed")
    except Exception as e:
        logger.warning("database_close_error", error=str(e))

    # Reset event bus
    reset_event_bus()
    logger.info("event_bus_reset")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="SocTalk API",
        description="Control plane API for SocTalk security investigation platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Configure CORS
    cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(auth_router, prefix="/api")
    app.include_router(analytics_router, prefix="/api", dependencies=[Depends(require_authenticated)])
    app.include_router(audit_router, prefix="/api", dependencies=[Depends(require_authenticated)])
    app.include_router(events_router, prefix="/api", dependencies=[Depends(require_authenticated)])
    app.include_router(investigations_router, prefix="/api", dependencies=[Depends(require_authenticated)])
    app.include_router(metrics_router, prefix="/api", dependencies=[Depends(require_authenticated)])
    app.include_router(review_router, prefix="/api", dependencies=[Depends(require_analyst)])
    app.include_router(settings_router, prefix="/api", dependencies=[Depends(require_authenticated)])

    # Health check endpoint
    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Basic health check endpoint."""
        return {"status": "healthy"}

    @app.get("/")
    async def root() -> dict[str, str]:
        """Root endpoint with API information."""
        return {
            "name": "SocTalk API",
            "version": "0.1.0",
            "docs": "/docs",
        }

    return app


# Create default app instance for uvicorn
app = create_app()
