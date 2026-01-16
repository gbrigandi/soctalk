"""MCP Client for connecting to Rust MCP servers via stdio transport.

Uses the official MCP Python SDK for reliable communication.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from soctalk.config import MCPServerConfig

logger = structlog.get_logger()


class MCPError(Exception):
    """Base exception for MCP errors."""
    pass


class MCPConnectionError(MCPError):
    """Error connecting to MCP server."""
    pass


class MCPToolError(MCPError):
    """Error executing MCP tool."""
    pass


class MCPClient:
    """Client for communicating with a single MCP server via stdio.

    Uses the official MCP Python SDK for reliable async communication.
    """

    def __init__(self, config: MCPServerConfig):
        """Initialize MCP client.

        Args:
            config: Configuration for the MCP server.
        """
        self.config = config
        self._session: Optional[ClientSession] = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._connected = False
        self._stdio_context = None
        self._session_context = None

    @property
    def name(self) -> str:
        """Get the server name."""
        return self.config.name

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected and self._session is not None

    async def connect(self) -> None:
        """Connect to the MCP server.

        Spawns the server process and performs MCP handshake.

        Raises:
            MCPConnectionError: If connection fails.
        """
        if self.is_connected:
            return

        server_path = self.config.path
        if not server_path.exists():
            raise MCPConnectionError(
                f"MCP server binary not found at {server_path}. "
                f"Please build the server with 'cargo build --release'"
            )

        try:
            # Prepare environment: inherit system env + add server-specific vars
            env = os.environ.copy()
            env.update(self.config.env_vars)

            logger.info(
                "starting_mcp_server",
                server=self.name,
                path=str(server_path),
                env_vars=list(self.config.env_vars.keys()),
            )

            # Create server parameters
            server_params = StdioServerParameters(
                command=str(server_path),
                args=[],
                env=env,
            )

            # Start the stdio client as context manager
            self._stdio_context = stdio_client(server_params)
            read_stream, write_stream = await self._stdio_context.__aenter__()

            # Create and enter client session context manager
            self._session_context = ClientSession(read_stream, write_stream)
            self._session = await self._session_context.__aenter__()

            # Initialize the session
            await self._session.initialize()

            # List available tools
            await self._list_tools()

            self._connected = True
            logger.info(
                "mcp_server_connected",
                server=self.name,
                tools_count=len(self._tools),
            )

        except Exception as e:
            await self.close()
            raise MCPConnectionError(f"Failed to connect to {self.name}: {e}") from e

    async def close(self) -> None:
        """Close the connection to the MCP server."""
        try:
            # Exit session context first
            if self._session_context:
                try:
                    await self._session_context.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning("error_closing_session", server=self.name, error=str(e))

            # Then exit stdio context
            if self._stdio_context:
                try:
                    await self._stdio_context.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning("error_closing_stdio", server=self.name, error=str(e))
        except Exception as e:
            logger.warning("error_closing_mcp_server", server=self.name, error=str(e))
        finally:
            self._session = None
            self._session_context = None
            self._stdio_context = None
            self._connected = False
            logger.info("mcp_server_disconnected", server=self.name)

    async def _list_tools(self) -> None:
        """List available tools from the server."""
        if not self._session:
            raise MCPConnectionError(f"Not connected to {self.name}")

        result = await self._session.list_tools()

        self._tools = {}
        for tool in result.tools:
            self._tools[tool.name] = {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.inputSchema,
            }
            logger.debug("mcp_tool_discovered", server=self.name, tool=tool.name)

    def get_available_tools(self) -> list[str]:
        """Get list of available tool names.

        Returns:
            List of tool names.
        """
        return list(self._tools.keys())

    def get_tool_schema(self, tool_name: str) -> Optional[dict[str, Any]]:
        """Get the schema for a specific tool.

        Args:
            tool_name: Name of the tool.

        Returns:
            Tool schema or None if not found.
        """
        return self._tools.get(tool_name)

    async def call_tool(self, tool_name: str, arguments: Optional[dict] = None) -> Any:
        """Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments to pass to the tool.

        Returns:
            The tool's result as text.

        Raises:
            MCPToolError: If the tool call fails.
        """
        if not self._session:
            raise MCPConnectionError(f"Not connected to {self.name}")

        if tool_name not in self._tools:
            available = ", ".join(self._tools.keys())
            raise MCPToolError(
                f"Tool '{tool_name}' not found on {self.name}. Available: {available}"
            )

        logger.info(
            "calling_mcp_tool",
            server=self.name,
            tool=tool_name,
            arguments=arguments,
        )

        try:
            result = await self._session.call_tool(tool_name, arguments or {})

            # Check for errors
            if result.isError:
                error_text = self._extract_text_content(result.content)
                raise MCPToolError(f"Tool {tool_name} failed: {error_text}")

            # Extract text content
            text_content = self._extract_text_content(result.content)

            logger.info(
                "mcp_tool_completed",
                server=self.name,
                tool=tool_name,
                result_length=len(text_content) if text_content else 0,
            )

            return text_content

        except MCPError:
            raise
        except Exception as e:
            raise MCPToolError(f"Error calling {tool_name} on {self.name}: {e}") from e

    def _extract_text_content(self, content: list) -> str:
        """Extract text from MCP content array.

        Args:
            content: List of content objects.

        Returns:
            Concatenated text content.
        """
        texts = []
        for item in content:
            if hasattr(item, 'text'):
                texts.append(item.text)
            elif hasattr(item, 'type') and item.type == 'text':
                texts.append(getattr(item, 'text', ''))
        return "\n".join(texts)


class MCPClientManager:
    """Manager for multiple MCP client connections.

    Handles lifecycle of all MCP server connections.
    """

    def __init__(self):
        """Initialize the client manager."""
        self._clients: dict[str, MCPClient] = {}

    async def add_client(self, config: MCPServerConfig) -> MCPClient:
        """Add and connect a new MCP client.

        Args:
            config: Configuration for the MCP server.

        Returns:
            The connected MCPClient instance.
        """
        client = MCPClient(config)
        await client.connect()
        self._clients[config.name] = client
        return client

    def get_client(self, name: str) -> Optional[MCPClient]:
        """Get a client by name.

        Args:
            name: Name of the MCP server.

        Returns:
            The MCPClient or None if not found.
        """
        return self._clients.get(name)

    async def close_all(self) -> None:
        """Close all client connections."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def get_all_clients(self) -> dict[str, MCPClient]:
        """Get all connected clients.

        Returns:
            Dictionary of client name to MCPClient.
        """
        return self._clients.copy()
