"""Abstract base class for HIL backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import structlog

from soctalk.hil.models import HILRequest, HILResponse

logger = structlog.get_logger()


class HILBackend(ABC):
    """Abstract base class for Human-in-the-Loop backends.

    Implementations must provide methods to:
    - Start/stop the backend connection
    - Send a review request and wait for response
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the backend name (e.g., 'slack', 'discord', 'cli')."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the backend is connected and ready."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start the backend connection.

        This should establish any persistent connections (WebSocket, etc.)
        and prepare the backend to send/receive messages.

        Raises:
            HILConnectionError: If connection fails.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the backend connection.

        This should gracefully close connections and clean up resources.
        """
        ...

    @abstractmethod
    async def request_approval(
        self,
        request: HILRequest,
        timeout: Optional[float] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> HILResponse:
        """Send a review request and wait for human response.

        This is the main method that:
        1. Formats and sends the investigation details to the human
        2. Waits for them to click a button (Approve/Reject/More Info)
        3. Returns their decision

        Args:
            request: The HIL request containing investigation details.
            timeout: Optional timeout in seconds. Defaults to request.timeout_seconds.
            state: Optional LangGraph state for conversational HIL inquiries.

        Returns:
            HILResponse with the human's decision.

        Raises:
            HILTimeoutError: If no response received within timeout.
            HILConnectionError: If connection lost during request.
        """
        ...


class HILError(Exception):
    """Base exception for HIL errors."""

    pass


class HILConnectionError(HILError):
    """Error connecting to or communicating with HIL backend."""

    pass


class HILTimeoutError(HILError):
    """Timeout waiting for human response."""

    pass
