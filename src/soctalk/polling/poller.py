"""Alert poller for continuous monitoring of Wazuh alerts."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Callable, Optional, Set

import structlog

from soctalk.config import get_config
from soctalk.models.alerts import Alert
from soctalk.workers.wazuh import poll_wazuh_alerts

logger = structlog.get_logger()


class AlertPoller:
    """Polls Wazuh for new alerts with buffered batch processing.

    Features:
    - Fetches alerts in bulk (e.g., 100 at a time)
    - Returns alerts in smaller batches (e.g., 5 at a time)
    - Deduplication of already-seen alerts
    - Priority sorting (critical first)
    - Only fetches more when buffer is empty
    """

    def __init__(
        self,
        interval_seconds: Optional[int] = None,
        max_alerts: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        """Initialize the alert poller.

        Args:
            interval_seconds: Polling interval. Defaults to config value.
            max_alerts: Max alerts to fetch from Wazuh. Defaults to config value.
            batch_size: Number of alerts to return per batch. Defaults to config value.
        """
        config = get_config()
        self.interval_seconds = interval_seconds or config.polling.interval_seconds
        self.max_alerts = max_alerts or config.polling.max_alerts_per_poll
        self.batch_size = batch_size or config.polling.batch_size

        self._seen_alert_ids: Set[str] = set()
        self._alert_buffer: deque[Alert] = deque()
        self._running = False
        self._poll_count = 0
        self._fetch_count = 0
        self._last_poll_time: Optional[datetime] = None

    async def poll(self) -> list[Alert]:
        """Get the next batch of alerts to process.

        If the buffer is empty, fetches new alerts from Wazuh.
        Returns up to batch_size alerts from the buffer.

        Returns:
            List of alerts (up to batch_size), sorted by severity.
        """
        self._poll_count += 1
        self._last_poll_time = datetime.now()

        # If buffer is empty, fetch more alerts
        if not self._alert_buffer:
            await self._fetch_alerts()

        # Return a batch from the buffer
        batch = []
        while self._alert_buffer and len(batch) < self.batch_size:
            batch.append(self._alert_buffer.popleft())

        logger.info(
            "batch_returned",
            batch_size=len(batch),
            remaining_in_buffer=len(self._alert_buffer),
            poll_count=self._poll_count,
        )

        return batch

    async def _fetch_alerts(self) -> None:
        """Fetch alerts from Wazuh and add new ones to the buffer."""
        self._fetch_count += 1

        logger.info(
            "fetching_alerts",
            fetch_count=self._fetch_count,
            max_alerts=self.max_alerts,
        )

        try:
            all_alerts = await poll_wazuh_alerts(limit=self.max_alerts)

            # Filter out already-seen alerts
            new_alerts = []
            for alert in all_alerts:
                if alert.id not in self._seen_alert_ids:
                    new_alerts.append(alert)
                    self._seen_alert_ids.add(alert.id)

            # Sort by severity (critical first)
            severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            new_alerts.sort(key=lambda a: severity_order.get(a.severity.value, 4))

            # Add to buffer
            self._alert_buffer.extend(new_alerts)

            logger.info(
                "alerts_fetched",
                total_from_wazuh=len(all_alerts),
                new_alerts=len(new_alerts),
                buffer_size=len(self._alert_buffer),
                seen_cache_size=len(self._seen_alert_ids),
            )

        except Exception as e:
            logger.error("fetch_error", error=str(e))

    def has_pending_alerts(self) -> bool:
        """Check if there are alerts in the buffer waiting to be processed."""
        return len(self._alert_buffer) > 0

    async def run_continuous(
        self,
        callback: Callable[[list[Alert]], None],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Run continuous polling loop.

        Args:
            callback: Async function to call with new alerts.
            stop_event: Optional event to signal stop.
        """
        self._running = True
        logger.info(
            "starting_continuous_polling",
            interval=self.interval_seconds,
            max_alerts=self.max_alerts,
            batch_size=self.batch_size,
        )

        try:
            while self._running:
                # Check stop event
                if stop_event and stop_event.is_set():
                    break

                # Poll for alerts
                alerts = await self.poll()

                # Call callback if we have new alerts
                if alerts:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(alerts)
                        else:
                            callback(alerts)
                    except Exception as e:
                        logger.error("callback_error", error=str(e))

                # Only wait if buffer is empty (no more to process immediately)
                if not self._alert_buffer:
                    await asyncio.sleep(self.interval_seconds)

        except asyncio.CancelledError:
            logger.info("polling_cancelled")
        finally:
            self._running = False
            logger.info("polling_stopped")

    def stop(self) -> None:
        """Stop the continuous polling loop."""
        self._running = False

    def clear_seen_cache(self) -> None:
        """Clear the seen alerts cache."""
        self._seen_alert_ids.clear()
        logger.info("seen_cache_cleared")

    def clear_buffer(self) -> None:
        """Clear the alert buffer."""
        self._alert_buffer.clear()
        logger.info("buffer_cleared")

    def mark_as_seen(self, alert_id: str) -> None:
        """Mark an alert as seen.

        Args:
            alert_id: Alert ID to mark as seen.
        """
        self._seen_alert_ids.add(alert_id)

    @property
    def is_running(self) -> bool:
        """Check if poller is running."""
        return self._running

    @property
    def poll_count(self) -> int:
        """Get total number of poll calls."""
        return self._poll_count

    @property
    def fetch_count(self) -> int:
        """Get total number of Wazuh fetches."""
        return self._fetch_count

    @property
    def buffer_size(self) -> int:
        """Get current buffer size."""
        return len(self._alert_buffer)

    @property
    def last_poll_time(self) -> Optional[datetime]:
        """Get timestamp of last poll."""
        return self._last_poll_time
