"""Human-in-the-Loop (HIL) module for SocTalk.

Provides pluggable backends for human review interactions via
different chat platforms (Slack, Discord, CLI, etc.).
"""

from soctalk.hil.models import HILRequest, HILResponse
from soctalk.hil.base import HILBackend
from soctalk.hil.service import HILService

__all__ = [
    "HILBackend",
    "HILRequest",
    "HILResponse",
    "HILService",
]
