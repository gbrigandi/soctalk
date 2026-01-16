"""In-memory event bus for real-time SSE broadcasting.

This module provides a simple publish-subscribe mechanism for broadcasting
events to connected SSE clients.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, AsyncIterator
from uuid import UUID, uuid4

import structlog

logger = structlog.get_logger()


@dataclass
class BroadcastEvent:
    """An event to be broadcast to SSE clients."""

    id: str
    event_type: str
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_sse(self) -> str:
        """Format as Server-Sent Event."""
        import json

        lines = [
            f"id: {self.id}",
            f"event: {self.event_type}",
            f"data: {json.dumps(self.data)}",
            "",  # Empty line to end the event
        ]
        return "\n".join(lines)


class EventBus:
    """In-memory event bus for broadcasting events to SSE clients.

    This is a simple pub-sub implementation that allows:
    - Publishing events from any part of the application
    - Subscribing to events (creates an async queue for the subscriber)
    - Automatic cleanup when subscribers disconnect

    Usage:
        bus = EventBus()

        # Subscribe
        async for event in bus.subscribe():
            print(event)

        # Publish
        await bus.publish("investigation.created", {"id": "..."})
    """

    def __init__(self, max_queue_size: int = 100):
        """Initialize the event bus.

        Args:
            max_queue_size: Maximum number of events to buffer per subscriber.
        """
        self._subscribers: dict[str, asyncio.Queue[BroadcastEvent]] = {}
        self._max_queue_size = max_queue_size
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None = None,
    ) -> None:
        """Publish an event to all subscribers.

        Args:
            event_type: Type of event (e.g., "investigation.created").
            data: Event data payload.
            event_id: Optional event ID (auto-generated if not provided).
        """
        event = BroadcastEvent(
            id=event_id or str(uuid4()),
            event_type=event_type,
            data=data,
        )

        async with self._lock:
            subscribers = list(self._subscribers.items())

        for subscriber_id, queue in subscribers:
            try:
                # Non-blocking put - drop events if queue is full
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "subscriber_queue_full",
                    subscriber_id=subscriber_id,
                    event_type=event_type,
                )

        logger.debug(
            "event_published",
            event_type=event_type,
            subscriber_count=len(subscribers),
        )

    async def subscribe(self) -> AsyncGenerator[BroadcastEvent, None]:
        """Subscribe to events.

        Yields:
            BroadcastEvent objects as they are published.
        """
        subscriber_id = str(uuid4())
        queue: asyncio.Queue[BroadcastEvent] = asyncio.Queue(
            maxsize=self._max_queue_size
        )

        async with self._lock:
            self._subscribers[subscriber_id] = queue

        logger.info("subscriber_connected", subscriber_id=subscriber_id)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                self._subscribers.pop(subscriber_id, None)
            logger.info("subscriber_disconnected", subscriber_id=subscriber_id)

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[tuple[str, asyncio.Queue[BroadcastEvent]]]:
        """Create a subscription queue for a single subscriber.

        Useful for integrations that need to manage queue reads manually
        (e.g., SSE heartbeat timeouts).
        """
        subscriber_id = str(uuid4())
        queue: asyncio.Queue[BroadcastEvent] = asyncio.Queue(
            maxsize=self._max_queue_size
        )

        async with self._lock:
            self._subscribers[subscriber_id] = queue

        logger.info("subscriber_connected", subscriber_id=subscriber_id)

        try:
            yield subscriber_id, queue
        finally:
            async with self._lock:
                self._subscribers.pop(subscriber_id, None)
            logger.info("subscriber_disconnected", subscriber_id=subscriber_id)

    @property
    def subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        return len(self._subscribers)


# Global event bus instance
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance.

    Creates one if it doesn't exist.

    Returns:
        The global EventBus instance.
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def reset_event_bus() -> None:
    """Reset the global event bus (for testing)."""
    global _event_bus
    _event_bus = None
