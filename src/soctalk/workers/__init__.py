"""Worker nodes for SecOps agent."""

from soctalk.workers.wazuh import wazuh_worker_node
from soctalk.workers.cortex import cortex_worker_node
from soctalk.workers.thehive import thehive_worker_node

__all__ = [
    "wazuh_worker_node",
    "cortex_worker_node",
    "thehive_worker_node",
]
