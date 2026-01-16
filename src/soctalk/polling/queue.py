"""Investigation queue for managing pending investigations."""

from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog

from soctalk.models.enums import Severity
from soctalk.models.investigation import Investigation

logger = structlog.get_logger()

# How long to block duplicate titles (covers investigation processing time)
TITLE_BLOCK_MINUTES = 10


@dataclass(order=True)
class PrioritizedInvestigation:
    """Wrapper for investigation with priority ordering.

    Lower priority value = higher priority (processed first).
    """

    priority: int
    timestamp: datetime = field(compare=False)
    investigation: Investigation = field(compare=False)


class InvestigationQueue:
    """Priority queue for investigations.

    Implements:
    - Priority ordering (critical severity first)
    - Async get with blocking
    - Queue size limits
    - Investigation deduplication
    """

    def __init__(self, max_size: int = 100):
        """Initialize the queue.

        Args:
            max_size: Maximum queue size.
        """
        self.max_size = max_size
        self._heap: list[PrioritizedInvestigation] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Condition()
        self._seen_ids: set[str] = set()
        # Track titles with expiration timestamps to prevent duplicates
        self._title_block_until: dict[str, datetime] = {}

    def _severity_to_priority(self, severity: Severity) -> int:
        """Convert severity to priority value.

        Args:
            severity: Severity level.

        Returns:
            Priority value (lower = higher priority).
        """
        priority_map = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        return priority_map.get(severity, 4)

    async def add(self, investigation: Investigation) -> bool:
        """Add an investigation to the queue.

        Args:
            investigation: Investigation to add.

        Returns:
            True if added, False if queue full or duplicate.
        """
        async with self._lock:
            # Check for duplicates by ID
            if investigation.id in self._seen_ids:
                logger.debug("duplicate_investigation_skipped", id=investigation.id)
                return False

            # Check for duplicate titles (block similar investigations for a time window)
            title = investigation.title or ""
            now = datetime.now()
            if title:
                block_until = self._title_block_until.get(title)
                if block_until and now < block_until:
                    logger.info(
                        "duplicate_title_blocked",
                        id=investigation.id,
                        title=title[:50],
                        blocked_for_seconds=int((block_until - now).total_seconds()),
                    )
                    return False

            # Check queue size
            if len(self._heap) >= self.max_size:
                logger.warning("queue_full", max_size=self.max_size)
                return False

            # Calculate priority
            priority = self._severity_to_priority(investigation.max_severity)

            # Add to heap
            item = PrioritizedInvestigation(
                priority=priority,
                timestamp=now,
                investigation=investigation,
            )
            heapq.heappush(self._heap, item)
            self._seen_ids.add(investigation.id)

            # Block this title for the configured time window
            if title:
                self._title_block_until[title] = now + timedelta(minutes=TITLE_BLOCK_MINUTES)

            logger.info(
                "investigation_queued",
                id=investigation.id,
                priority=priority,
                severity=investigation.max_severity.value,
                queue_size=len(self._heap),
            )

        # Notify waiters
        async with self._not_empty:
            self._not_empty.notify()

        return True

    async def add_batch(self, investigations: list[Investigation]) -> int:
        """Add multiple investigations to the queue.

        Args:
            investigations: List of investigations to add.

        Returns:
            Number of investigations added.
        """
        added = 0
        for inv in investigations:
            if await self.add(inv):
                added += 1
        return added

    async def get(self, timeout: Optional[float] = None) -> Optional[Investigation]:
        """Get the highest priority investigation.

        Blocks until an investigation is available or timeout.

        Args:
            timeout: Maximum time to wait in seconds. None = wait forever.

        Returns:
            Investigation or None if timeout.
        """
        async with self._not_empty:
            # Wait for items if queue is empty
            while len(self._heap) == 0:
                try:
                    await asyncio.wait_for(
                        self._not_empty.wait(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return None

            # Pop highest priority item
            async with self._lock:
                if self._heap:
                    item = heapq.heappop(self._heap)
                    # Keep title in _pending_titles until investigation is completed
                    # This prevents duplicate investigations while one is being processed
                    logger.info(
                        "investigation_dequeued",
                        id=item.investigation.id,
                        priority=item.priority,
                        queue_size=len(self._heap),
                    )
                    return item.investigation

        return None

    async def peek(self) -> Optional[Investigation]:
        """Peek at the highest priority investigation without removing it.

        Returns:
            Investigation or None if queue empty.
        """
        async with self._lock:
            if self._heap:
                return self._heap[0].investigation
        return None

    async def size(self) -> int:
        """Get current queue size.

        Returns:
            Number of investigations in queue.
        """
        async with self._lock:
            return len(self._heap)

    async def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if empty.
        """
        return await self.size() == 0

    async def clear(self) -> None:
        """Clear all investigations from the queue."""
        async with self._lock:
            self._heap.clear()
            self._seen_ids.clear()
            self._title_block_until.clear()
            logger.info("queue_cleared")

    def mark_completed(self, investigation_id: str, title: Optional[str] = None) -> None:
        """Mark an investigation as completed.

        Title blocking is time-based, so we don't need to explicitly clear.
        This method is kept for compatibility and logging purposes.

        Args:
            investigation_id: ID of completed investigation.
            title: Title of the investigation (logged for debugging).
        """
        logger.debug(
            "investigation_marked_completed",
            investigation_id=investigation_id,
            title=title[:50] if title else None,
        )

    async def get_stats(self) -> dict:
        """Get queue statistics.

        Returns:
            Dictionary with queue stats.
        """
        async with self._lock:
            if not self._heap:
                return {
                    "size": 0,
                    "max_size": self.max_size,
                    "seen_count": len(self._seen_ids),
                }

            severity_counts = {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
            }

            for item in self._heap:
                sev = item.investigation.max_severity.value
                severity_counts[sev] = severity_counts.get(sev, 0) + 1

            return {
                "size": len(self._heap),
                "max_size": self.max_size,
                "seen_count": len(self._seen_ids),
                "by_severity": severity_counts,
            }
