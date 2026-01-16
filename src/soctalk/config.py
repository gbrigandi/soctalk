"""Configuration management for SocTalk agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    name: str
    path: Path
    env_vars: dict[str, str] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    """Configuration for LLM models."""

    provider: Literal["anthropic", "openai"] = "anthropic"
    fast_model: str = "claude-sonnet-4-20250514"
    reasoning_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: str = ""
    anthropic_base_url: Optional[str] = None
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    openai_organization: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = 4096


class PollingConfig(BaseModel):
    """Configuration for alert polling."""

    interval_seconds: int = 30
    max_alerts_per_poll: int = 100
    batch_size: int = 5  # Process alerts in batches of this size
    correlation_window_minutes: int = 15


class ThresholdsConfig(BaseModel):
    """Configuration for decision thresholds."""

    auto_close_confidence: float = 0.25
    escalation_confidence: float = 0.50
    critical_severity_level: int = 12  # Wazuh severity 12+ is critical


class HILConfig(BaseModel):
    """Configuration for Human-in-the-Loop backend."""

    backend: str = "cli"  # 'dashboard', 'cli', 'slack', 'discord'
    enabled: bool = True
    timeout_seconds: int = 300  # 5 minutes default

    # Slack-specific settings
    slack_bot_token: Optional[str] = None
    slack_app_token: Optional[str] = None
    slack_channel: Optional[str] = None

    # Discord-specific settings (for future use)
    discord_bot_token: Optional[str] = None
    discord_channel_id: Optional[int] = None


class DatabaseConfig(BaseModel):
    """Configuration for database (event sourcing persistence)."""

    enabled: bool = False
    url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/soctalk"


class Config(BaseModel):
    """Main configuration for SocTalk agent."""

    # MCP Server paths
    wazuh_mcp_server: MCPServerConfig
    cortex_mcp_server: MCPServerConfig
    thehive_mcp_server: MCPServerConfig
    misp_mcp_server: MCPServerConfig

    # LLM settings
    llm: LLMConfig

    # Polling settings
    polling: PollingConfig

    # Decision thresholds
    thresholds: ThresholdsConfig

    # Human-in-the-Loop settings
    hil: HILConfig

    # Database settings (event sourcing)
    database: Optional[DatabaseConfig] = None

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"


def load_config(env_file: Optional[Path] = None) -> Config:
    """Load configuration from environment variables.

    Args:
        env_file: Optional path to .env file. Defaults to .env in current directory.

    Returns:
        Config: Loaded configuration object.

    Raises:
        ValueError: If required configuration is missing.
    """
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    # Get base path for MCP servers (relative to this project)
    base_path = Path(os.getenv("MCP_SERVERS_BASE_PATH", ".."))

    wazuh_url = os.getenv("WAZUH_URL")
    if wazuh_url and "://" not in wazuh_url:
        wazuh_url = f"https://{wazuh_url}"

    wazuh_parsed = urlsplit(wazuh_url) if wazuh_url else None
    wazuh_host = wazuh_parsed.hostname if wazuh_parsed and wazuh_parsed.hostname else os.getenv("WAZUH_API_HOST", "localhost")
    wazuh_port = str(wazuh_parsed.port) if wazuh_parsed and wazuh_parsed.port else os.getenv("WAZUH_API_PORT", "55000")

    # Wazuh MCP Server config
    wazuh_config = MCPServerConfig(
        name="wazuh",
        path=Path(
            os.getenv(
                "WAZUH_MCP_SERVER_PATH",
                str(base_path / "mcp-server-wazuh" / "target" / "release" / "mcp-server-wazuh"),
            )
        ),
        env_vars={
            "WAZUH_API_HOST": wazuh_host,
            "WAZUH_API_PORT": wazuh_port,
            "WAZUH_API_USERNAME": os.getenv("WAZUH_API_USER") or os.getenv("WAZUH_API_USERNAME", "wazuh"),
            "WAZUH_API_PASSWORD": os.getenv("WAZUH_API_PASSWORD", "wazuh"),
            "WAZUH_INDEXER_HOST": os.getenv("WAZUH_INDEXER_HOST", "localhost"),
            "WAZUH_INDEXER_PORT": os.getenv("WAZUH_INDEXER_PORT", "9200"),
            "WAZUH_INDEXER_USERNAME": os.getenv("WAZUH_INDEXER_USERNAME", "admin"),
            "WAZUH_INDEXER_PASSWORD": os.getenv("WAZUH_INDEXER_PASSWORD", "admin"),
            "WAZUH_VERIFY_SSL": os.getenv("WAZUH_VERIFY_SSL", "false"),
        },
    )

    cortex_endpoint = os.getenv("CORTEX_URL") or os.getenv("CORTEX_ENDPOINT", "http://localhost:9000/api")

    # Cortex MCP Server config
    cortex_config = MCPServerConfig(
        name="cortex",
        path=Path(
            os.getenv(
                "CORTEX_MCP_SERVER_PATH",
                str(base_path / "mcp-server-cortex" / "target" / "release" / "mcp-server-cortex"),
            )
        ),
        env_vars={
            "CORTEX_ENDPOINT": cortex_endpoint,
            "CORTEX_API_KEY": os.getenv("CORTEX_API_KEY", ""),
        },
    )

    thehive_token = os.getenv("THEHIVE_API_KEY") or os.getenv("THEHIVE_API_TOKEN", "")

    # TheHive MCP Server config
    thehive_config = MCPServerConfig(
        name="thehive",
        path=Path(
            os.getenv(
                "THEHIVE_MCP_SERVER_PATH",
                str(base_path / "mcp-server-thehive" / "target" / "release" / "mcp-server-thehive"),
            )
        ),
        env_vars={
            "THEHIVE_URL": os.getenv("THEHIVE_URL", "http://localhost:9000/api"),
            "THEHIVE_API_TOKEN": thehive_token,
            "VERIFY_SSL": os.getenv("THEHIVE_VERIFY_SSL", "false"),
        },
    )

    # MISP MCP Server config
    misp_config = MCPServerConfig(
        name="misp",
        path=Path(
            os.getenv(
                "MISP_MCP_SERVER_PATH",
                str(base_path / "mcp-server-misp" / "target" / "release" / "mcp-server-misp"),
            )
        ),
        env_vars={
            "MISP_URL": os.getenv("MISP_URL", "https://localhost"),
            "MISP_API_KEY": os.getenv("MISP_API_KEY", ""),
            "MISP_VERIFY_SSL": os.getenv("MISP_VERIFY_SSL", "false"),
        },
    )

    def _optional_env(name: str) -> Optional[str]:
        value = os.getenv(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    # LLM config (Anthropic or OpenAI-compatible; mutually exclusive)
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()

    provider_preference = (os.getenv("SOCTALK_LLM_PROVIDER") or "").strip().lower()
    if provider_preference and provider_preference not in {"anthropic", "openai"}:
        raise ValueError(
            f"Invalid SOCTALK_LLM_PROVIDER={provider_preference!r}. Expected 'anthropic' or 'openai'."
        )

    if anthropic_api_key and openai_api_key:
        raise ValueError(
            "Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set. "
            "SocTalk supports either Anthropic or an OpenAI-compatible provider (mutually exclusive). "
            "Unset one of the keys."
        )

    provider = provider_preference or ("openai" if openai_api_key else "anthropic")

    # Auto-correct provider if a key is present for the other provider.
    if provider == "anthropic" and not anthropic_api_key and openai_api_key:
        provider = "openai"
    if provider == "openai" and not openai_api_key and anthropic_api_key:
        provider = "anthropic"

    if provider == "anthropic" and not anthropic_api_key:
        raise ValueError(
            "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY (mutually exclusive)."
        )
    if provider == "openai" and not openai_api_key:
        raise ValueError(
            "No LLM API key configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY (mutually exclusive)."
        )

    # LLM config
    llm_config = LLMConfig(
        provider=provider,  # type: ignore[arg-type]
        fast_model=os.getenv("SOCTALK_FAST_MODEL", "claude-sonnet-4-20250514"),
        reasoning_model=os.getenv("SOCTALK_REASONING_MODEL", "claude-sonnet-4-20250514"),
        anthropic_api_key=anthropic_api_key,
        anthropic_base_url=_optional_env("ANTHROPIC_BASE_URL"),
        openai_api_key=openai_api_key,
        openai_base_url=_optional_env("OPENAI_BASE_URL") or _optional_env("OPENAI_API_BASE"),
        openai_organization=_optional_env("OPENAI_ORGANIZATION"),
        temperature=float(os.getenv("SOCTALK_LLM_TEMPERATURE", "0.0")),
        max_tokens=int(os.getenv("SOCTALK_LLM_MAX_TOKENS", "4096")),
    )

    # Polling config
    polling_config = PollingConfig(
        interval_seconds=int(os.getenv("SOCTALK_POLLING_INTERVAL", "30")),
        max_alerts_per_poll=int(os.getenv("SOCTALK_MAX_ALERTS_PER_POLL", "100")),
        batch_size=int(os.getenv("SOCTALK_BATCH_SIZE", "5")),
        correlation_window_minutes=int(os.getenv("SOCTALK_CORRELATION_WINDOW", "15")),
    )

    # Thresholds config
    thresholds_config = ThresholdsConfig(
        auto_close_confidence=float(os.getenv("SOCTALK_AUTO_CLOSE_THRESHOLD", "0.25")),
        escalation_confidence=float(os.getenv("SOCTALK_ESCALATION_THRESHOLD", "0.50")),
        critical_severity_level=int(os.getenv("SOCTALK_CRITICAL_SEVERITY", "12")),
    )

    # HIL config
    discord_channel_str = os.getenv("SOCTALK_HIL_DISCORD_CHANNEL_ID")
    hil_config = HILConfig(
        backend=os.getenv("SOCTALK_HIL_BACKEND", "cli"),
        enabled=os.getenv("SOCTALK_HIL_ENABLED", "true").lower() == "true",
        timeout_seconds=int(os.getenv("SOCTALK_HIL_TIMEOUT", "300")),
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN"),
        slack_app_token=os.getenv("SLACK_APP_TOKEN"),
        slack_channel=os.getenv("SOCTALK_HIL_SLACK_CHANNEL"),
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN"),
        discord_channel_id=int(discord_channel_str) if discord_channel_str else None,
    )

    # Database config (optional - event sourcing persistence)
    db_enabled = os.getenv("SOCTALK_DB_ENABLED", "false").lower() == "true"
    database_config = None
    if db_enabled:
        database_config = DatabaseConfig(
            enabled=True,
            url=os.getenv(
                "SOCTALK_DATABASE_URL",
                "postgresql+asyncpg://postgres:postgres@localhost:5432/soctalk",
            ),
        )

    return Config(
        wazuh_mcp_server=wazuh_config,
        cortex_mcp_server=cortex_config,
        thehive_mcp_server=thehive_config,
        misp_mcp_server=misp_config,
        llm=llm_config,
        polling=polling_config,
        thresholds=thresholds_config,
        hil=hil_config,
        database=database_config,
        log_level=os.getenv("SOCTALK_LOG_LEVEL", "INFO"),
        log_format=os.getenv("SOCTALK_LOG_FORMAT", "json"),
    )


# Global config instance (lazy loaded)
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance.

    Returns:
        Config: The global configuration object.
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config
