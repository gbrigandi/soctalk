"""Global MCP client bindings for the SocTalk agent.

This module provides startup/shutdown lifecycle management for MCP clients,
following the pattern from Google's mcp-security project.

Now supports database-backed settings from the Settings UI.
"""

from __future__ import annotations

import os
from typing import Optional

import structlog

from soctalk.config import get_config
from soctalk.mcp.client import MCPClient, MCPClientManager
from soctalk.settings_provider import EnabledMCPServers, create_mcp_configs, load_integration_settings_from_env

logger = structlog.get_logger()

# Global client instances
_manager: Optional[MCPClientManager] = None
_wazuh_client: Optional[MCPClient] = None
_cortex_client: Optional[MCPClient] = None
_thehive_client: Optional[MCPClient] = None
_misp_client: Optional[MCPClient] = None


async def bind_clients(mcp_configs: Optional[EnabledMCPServers] = None) -> None:
    """Initialize and connect all MCP clients.

    This should be called at application startup.

    Args:
        mcp_configs: Optional MCP server configurations from database settings.
                    If None, falls back to environment-based config.

    Raises:
        Exception: If any client fails to connect.
    """
    global _manager, _wazuh_client, _cortex_client, _thehive_client, _misp_client

    logger.info("binding_mcp_clients")

    _manager = MCPClientManager()

    if mcp_configs is not None:
        # Use database-backed settings
        await _bind_from_db_settings(mcp_configs)
    else:
        # Fall back to environment-based config
        await _bind_from_env_config()

    # Log summary
    connected = []
    if _wazuh_client:
        connected.append("wazuh")
    if _cortex_client:
        connected.append("cortex")
    if _thehive_client:
        connected.append("thehive")
    if _misp_client:
        connected.append("misp")

    logger.info(
        "mcp_clients_bound",
        connected=connected,
        count=len(connected),
    )


async def _bind_from_db_settings(mcp_configs: EnabledMCPServers) -> None:
    """Bind MCP clients based on database settings.

    Only connects to servers that are enabled in the Settings UI.

    Args:
        mcp_configs: MCP server configurations from database settings.
    """
    global _wazuh_client, _cortex_client, _thehive_client, _misp_client

    # Connect to Wazuh MCP server (if enabled)
    if mcp_configs.wazuh:
        logger.info("connecting_to_wazuh", config="database_settings")
        try:
            _wazuh_client = await _manager.add_client(mcp_configs.wazuh)
            logger.info("wazuh_connected", tools=_wazuh_client.get_available_tools())
        except Exception as e:
            logger.error("wazuh_connection_failed", error=str(e))
    else:
        logger.info("wazuh_disabled_in_settings")

    # Connect to Cortex MCP server (if enabled)
    if mcp_configs.cortex:
        logger.info("connecting_to_cortex", config="database_settings")
        try:
            _cortex_client = await _manager.add_client(mcp_configs.cortex)
            logger.info("cortex_connected", tools=_cortex_client.get_available_tools())
        except Exception as e:
            logger.error("cortex_connection_failed", error=str(e))
    else:
        logger.info("cortex_disabled_in_settings")

    # Connect to TheHive MCP server (if enabled)
    if mcp_configs.thehive:
        logger.info("connecting_to_thehive", config="database_settings")
        try:
            _thehive_client = await _manager.add_client(mcp_configs.thehive)
            logger.info("thehive_connected", tools=_thehive_client.get_available_tools())
        except Exception as e:
            logger.error("thehive_connection_failed", error=str(e))
    else:
        logger.info("thehive_disabled_in_settings")

    # Connect to MISP MCP server (if enabled)
    if mcp_configs.misp:
        logger.info("connecting_to_misp", config="database_settings")
        try:
            _misp_client = await _manager.add_client(mcp_configs.misp)
            logger.info("misp_connected", tools=_misp_client.get_available_tools())
        except Exception as e:
            logger.error("misp_connection_failed", error=str(e))
    else:
        logger.info("misp_disabled_in_settings")


async def _bind_from_env_config() -> None:
    """Bind MCP clients based on environment configuration.

    This is the legacy fallback when database is not available.
    """
    global _wazuh_client, _cortex_client, _thehive_client, _misp_client

    explicit_flags = any(
        os.getenv(name) is not None
        for name in ["WAZUH_ENABLED", "CORTEX_ENABLED", "THEHIVE_ENABLED", "MISP_ENABLED"]
    )

    if explicit_flags:
        logger.info("using_env_flags_for_mcp_binding")
        env_settings = load_integration_settings_from_env()
        env_configs = create_mcp_configs(env_settings)
        await _bind_from_db_settings(env_configs)
        return

    config = get_config()
    logger.info("using_legacy_env_config_fallback")

    logger.info("connecting_to_wazuh", config="environment")
    _wazuh_client = await _manager.add_client(config.wazuh_mcp_server)

    logger.info("connecting_to_cortex", config="environment")
    _cortex_client = await _manager.add_client(config.cortex_mcp_server)

    logger.info("connecting_to_thehive", config="environment")
    _thehive_client = await _manager.add_client(config.thehive_mcp_server)

    logger.info("connecting_to_misp", config="environment")
    _misp_client = await _manager.add_client(config.misp_mcp_server)

    logger.info(
        "mcp_clients_bound_from_env",
        wazuh_tools=_wazuh_client.get_available_tools() if _wazuh_client else [],
        cortex_tools=_cortex_client.get_available_tools() if _cortex_client else [],
        thehive_tools=_thehive_client.get_available_tools() if _thehive_client else [],
        misp_tools=_misp_client.get_available_tools() if _misp_client else [],
    )


async def cleanup_clients() -> None:
    """Close all MCP client connections.

    This should be called at application shutdown.
    """
    global _manager, _wazuh_client, _cortex_client, _thehive_client, _misp_client

    logger.info("cleaning_up_mcp_clients")

    if _manager:
        await _manager.close_all()

    _manager = None
    _wazuh_client = None
    _cortex_client = None
    _thehive_client = None
    _misp_client = None

    logger.info("mcp_clients_cleaned_up")


def get_wazuh_client() -> Optional[MCPClient]:
    """Get the Wazuh MCP client.

    Returns:
        The Wazuh MCPClient instance, or None if not connected.
    """
    return _wazuh_client


def get_cortex_client() -> Optional[MCPClient]:
    """Get the Cortex MCP client.

    Returns:
        The Cortex MCPClient instance, or None if not connected.
    """
    return _cortex_client


def get_thehive_client() -> Optional[MCPClient]:
    """Get the TheHive MCP client.

    Returns:
        The TheHive MCPClient instance, or None if not connected.
    """
    return _thehive_client


def get_manager() -> Optional[MCPClientManager]:
    """Get the MCP client manager.

    Returns:
        The MCPClientManager instance, or None if not initialized.
    """
    return _manager


def get_misp_client() -> Optional[MCPClient]:
    """Get the MISP MCP client.

    Returns:
        The MISP MCPClient instance, or None if not connected.
    """
    return _misp_client


def is_wazuh_enabled() -> bool:
    """Check if Wazuh integration is enabled and connected."""
    return _wazuh_client is not None


def is_cortex_enabled() -> bool:
    """Check if Cortex integration is enabled and connected."""
    return _cortex_client is not None


def is_thehive_enabled() -> bool:
    """Check if TheHive integration is enabled and connected."""
    return _thehive_client is not None


def is_misp_enabled() -> bool:
    """Check if MISP integration is enabled and connected."""
    return _misp_client is not None


def get_enabled_integrations() -> list[str]:
    """Get list of enabled integration names."""
    enabled = []
    if _wazuh_client:
        enabled.append("wazuh")
    if _cortex_client:
        enabled.append("cortex")
    if _thehive_client:
        enabled.append("thehive")
    if _misp_client:
        enabled.append("misp")
    return enabled
