"""Supervisor module for SecOps agent orchestration."""

from soctalk.supervisor.node import supervisor_node
from soctalk.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT

__all__ = [
    "supervisor_node",
    "SUPERVISOR_SYSTEM_PROMPT",
]
