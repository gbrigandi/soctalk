"""FastAPI dependency injection for database and services."""

from __future__ import annotations

import structlog
from typing import Annotated, AsyncGenerator

from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.api.event_bus import EventBus, get_event_bus
from soctalk.persistence.database import get_async_session_factory

logger = structlog.get_logger()


class DatabaseNotAvailableError(Exception):
    """Raised when database connection is not available."""

    pass


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session for FastAPI dependency injection.

    Yields:
        AsyncSession that auto-commits on success, auto-rollbacks on failure.

    Raises:
        HTTPException: 503 if database connection fails.
    """
    try:
        factory = get_async_session_factory()
        async with factory() as session:
            # Test the connection is actually working
            try:
                await session.execute(text("SELECT 1"))
            except Exception as e:
                logger.warning("database_connection_failed", error=str(e))
                raise HTTPException(
                    status_code=503,
                    detail="Database not available. Please ensure PostgreSQL is running.",
                )
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("database_session_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail="Database not available. Please ensure PostgreSQL is running.",
        )


# Type aliases for cleaner dependency injection
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
