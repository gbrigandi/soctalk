"""LangGraph checkpointing with PostgreSQL for workflow resumption."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

logger = structlog.get_logger()


def get_checkpoint_connection_string() -> str:
    """Get the PostgreSQL connection string for checkpointing.

    Returns the DATABASE_URL with asyncpg driver replaced with psycopg
    (LangGraph checkpoint uses psycopg, not asyncpg).
    """
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://soctalk:soctalk@localhost:5432/soctalk",
    )
    # LangGraph's AsyncPostgresSaver uses psycopg (async mode), not asyncpg
    # Convert from SQLAlchemy format to standard PostgreSQL URL
    if "+asyncpg" in url:
        url = url.replace("postgresql+asyncpg://", "postgresql://")
    elif "+psycopg2" in url:
        url = url.replace("postgresql+psycopg2://", "postgresql://")
    return url


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[AsyncPostgresSaver, None]:
    """Get an async checkpointer for LangGraph workflows.

    This creates a PostgreSQL-backed checkpointer that enables:
    - Workflow state persistence
    - Investigation resumption after interruption
    - Human-in-the-loop workflow pausing

    Yields:
        Configured AsyncPostgresSaver instance

    Example:
        async with get_checkpointer() as checkpointer:
            graph = build_secops_graph(checkpointer=checkpointer)
            result = await graph.ainvoke(state, config={"configurable": {"thread_id": "inv-123"}})
    """
    connection_string = get_checkpoint_connection_string()

    logger.debug("initializing_checkpointer", connection_string=connection_string[:50] + "...")

    async with AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        # Setup creates the checkpoint tables if they don't exist
        await checkpointer.setup()
        logger.info("checkpointer_initialized")
        yield checkpointer


async def create_checkpointer() -> AsyncPostgresSaver:
    """Create a standalone checkpointer instance.

    This is useful when you need to manage the checkpointer lifecycle manually.
    Remember to call setup() after creating and close connections when done.

    Returns:
        AsyncPostgresSaver instance (not yet setup)

    Example:
        checkpointer = await create_checkpointer()
        await checkpointer.setup()
        # ... use checkpointer ...
    """
    connection_string = get_checkpoint_connection_string()
    checkpointer = AsyncPostgresSaver.from_conn_string(connection_string)
    return checkpointer


def get_thread_id_for_investigation(investigation_id: str) -> str:
    """Generate a consistent thread ID for an investigation.

    This ensures the same investigation always uses the same thread,
    allowing workflow resumption.

    Args:
        investigation_id: UUID of the investigation

    Returns:
        Thread ID string for LangGraph config
    """
    return f"investigation-{investigation_id}"


def get_checkpoint_config(investigation_id: str) -> dict:
    """Get the LangGraph config for checkpointing an investigation.

    Args:
        investigation_id: UUID of the investigation

    Returns:
        Config dict with thread_id for LangGraph
    """
    return {
        "configurable": {
            "thread_id": get_thread_id_for_investigation(investigation_id),
        }
    }
