"""MCP client infrastructure for connecting to Rust MCP servers."""

from soctalk.mcp.client import MCPClient, MCPClientManager
from soctalk.mcp.bindings import bind_clients, cleanup_clients, get_wazuh_client, get_cortex_client, get_thehive_client

__all__ = [
    "MCPClient",
    "MCPClientManager",
    "bind_clients",
    "cleanup_clients",
    "get_wazuh_client",
    "get_cortex_client",
    "get_thehive_client",
]
