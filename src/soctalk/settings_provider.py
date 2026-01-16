"""Integration settings for MCP servers and notifications.

The Settings UI persists non-secret runtime preferences (enabled flags, URLs, and
toggles) in the database. Secrets (API keys, passwords, webhook URLs) are read
from environment variables only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.config import MCPServerConfig
from soctalk.persistence.models import UserSettings

logger = structlog.get_logger()


@dataclass
class IntegrationSettings:
    """Non-secret integration settings (DB-backed, env-seeded)."""

    # Wazuh SIEM
    wazuh_enabled: bool = False
    wazuh_url: Optional[str] = None
    wazuh_verify_ssl: bool = True

    # Cortex
    cortex_enabled: bool = False
    cortex_url: Optional[str] = None
    cortex_verify_ssl: bool = True

    # TheHive
    thehive_enabled: bool = False
    thehive_url: Optional[str] = None
    thehive_organisation: Optional[str] = None
    thehive_verify_ssl: bool = True

    # MISP
    misp_enabled: bool = False
    misp_url: Optional[str] = None
    misp_verify_ssl: bool = True

    # Slack
    slack_enabled: bool = False
    slack_channel: Optional[str] = None
    slack_notify_on_escalation: bool = True
    slack_notify_on_verdict: bool = True


@dataclass(frozen=True)
class IntegrationSecrets:
    """Secret integration settings (env-only)."""

    wazuh_username: Optional[str] = None
    wazuh_password: Optional[str] = None
    cortex_api_key: Optional[str] = None
    thehive_api_key: Optional[str] = None
    misp_api_key: Optional[str] = None
    slack_webhook_url: Optional[str] = None


@dataclass
class LLMSettings:
    """Non-secret LLM settings (DB-backed, env-seeded)."""

    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_fast_model: str = "claude-sonnet-4-20250514"
    llm_reasoning_model: str = "claude-sonnet-4-20250514"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_anthropic_base_url: Optional[str] = None
    llm_openai_base_url: Optional[str] = None
    llm_openai_organization: Optional[str] = None


@dataclass(frozen=True)
class LLMSecrets:
    """Secret LLM settings (env-only)."""

    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_settings_readonly() -> bool:
    """Whether Settings UI edits are disabled for this environment."""
    return _parse_bool(os.getenv("SETTINGS_READONLY"), False)


def load_integration_settings_from_env() -> IntegrationSettings:
    """Load non-secret integration settings from environment variables."""
    wazuh_url = os.getenv("WAZUH_URL")
    if not wazuh_url:
        host = os.getenv("WAZUH_API_HOST")
        port = os.getenv("WAZUH_API_PORT")
        if host and port:
            wazuh_url = f"https://{host}:{port}"

    cortex_url = os.getenv("CORTEX_URL") or os.getenv("CORTEX_ENDPOINT")

    return IntegrationSettings(
        # Wazuh
        wazuh_enabled=_parse_bool(os.getenv("WAZUH_ENABLED"), False),
        wazuh_url=wazuh_url,
        wazuh_verify_ssl=_parse_bool(os.getenv("WAZUH_VERIFY_SSL"), True),
        # Cortex
        cortex_enabled=_parse_bool(os.getenv("CORTEX_ENABLED"), False),
        cortex_url=cortex_url,
        cortex_verify_ssl=_parse_bool(os.getenv("CORTEX_VERIFY_SSL"), True),
        # TheHive
        thehive_enabled=_parse_bool(os.getenv("THEHIVE_ENABLED"), False),
        thehive_url=os.getenv("THEHIVE_URL"),
        thehive_organisation=os.getenv("THEHIVE_ORGANISATION"),
        thehive_verify_ssl=_parse_bool(os.getenv("THEHIVE_VERIFY_SSL"), True),
        # MISP
        misp_enabled=_parse_bool(os.getenv("MISP_ENABLED"), False),
        misp_url=os.getenv("MISP_URL"),
        misp_verify_ssl=_parse_bool(os.getenv("MISP_VERIFY_SSL"), True),
        # Slack
        slack_enabled=_parse_bool(os.getenv("SLACK_ENABLED"), False),
        slack_channel=os.getenv("SLACK_CHANNEL"),
        slack_notify_on_escalation=_parse_bool(os.getenv("SLACK_NOTIFY_ON_ESCALATION"), True),
        slack_notify_on_verdict=_parse_bool(os.getenv("SLACK_NOTIFY_ON_VERDICT"), True),
    )


def load_integration_secrets_from_env() -> IntegrationSecrets:
    """Load secret integration settings from environment variables."""
    return IntegrationSecrets(
        wazuh_username=os.getenv("WAZUH_API_USER") or os.getenv("WAZUH_API_USERNAME"),
        wazuh_password=os.getenv("WAZUH_API_PASSWORD"),
        cortex_api_key=os.getenv("CORTEX_API_KEY"),
        thehive_api_key=os.getenv("THEHIVE_API_KEY") or os.getenv("THEHIVE_API_TOKEN"),
        misp_api_key=os.getenv("MISP_API_KEY"),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL"),
    )


def load_llm_settings_from_env() -> LLMSettings:
    """Load non-secret LLM settings from environment variables."""
    provider = (os.getenv("SOCTALK_LLM_PROVIDER") or "").strip().lower()
    anthropic_key_set = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    openai_key_set = bool((os.getenv("OPENAI_API_KEY") or "").strip())

    if provider not in {"anthropic", "openai"}:
        provider = "openai" if openai_key_set and not anthropic_key_set else "anthropic"

    openai_base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    openai_base_url = openai_base_url.strip() if openai_base_url else None

    return LLMSettings(
        llm_provider=provider,  # type: ignore[arg-type]
        llm_fast_model=os.getenv("SOCTALK_FAST_MODEL", "claude-sonnet-4-20250514"),
        llm_reasoning_model=os.getenv("SOCTALK_REASONING_MODEL", "claude-sonnet-4-20250514"),
        llm_temperature=float(os.getenv("SOCTALK_LLM_TEMPERATURE", "0.0")),
        llm_max_tokens=int(os.getenv("SOCTALK_LLM_MAX_TOKENS", "4096")),
        llm_anthropic_base_url=(os.getenv("ANTHROPIC_BASE_URL") or "").strip() or None,
        llm_openai_base_url=openai_base_url or None,
        llm_openai_organization=(os.getenv("OPENAI_ORGANIZATION") or "").strip() or None,
    )


def load_llm_secrets_from_env() -> LLMSecrets:
    """Load secret LLM settings from environment variables."""
    anthropic_api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip() or None
    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip() or None
    return LLMSecrets(anthropic_api_key=anthropic_api_key, openai_api_key=openai_api_key)


async def seed_settings_from_env(
    session: AsyncSession,
    *,
    overwrite: bool,
) -> UserSettings:
    """Seed the settings table from environment variables.

    - If no settings row exists, creates one from env values.
    - If ``overwrite`` is True, updates existing settings with env values.
    """
    env_settings = load_integration_settings_from_env()
    env_llm_settings = load_llm_settings_from_env()

    result = await session.execute(select(UserSettings).where(UserSettings.id == "default"))
    settings = result.scalar_one_or_none()
    created = False

    if settings is None:
        settings = UserSettings(id="default")
        session.add(settings)
        overwrite = True
        created = True

    if overwrite:
        changed = created
        updates = {
            "wazuh_enabled": env_settings.wazuh_enabled,
            "wazuh_url": env_settings.wazuh_url,
            "wazuh_verify_ssl": env_settings.wazuh_verify_ssl,
            "cortex_enabled": env_settings.cortex_enabled,
            "cortex_url": env_settings.cortex_url,
            "cortex_verify_ssl": env_settings.cortex_verify_ssl,
            "thehive_enabled": env_settings.thehive_enabled,
            "thehive_url": env_settings.thehive_url,
            "thehive_organisation": env_settings.thehive_organisation,
            "thehive_verify_ssl": env_settings.thehive_verify_ssl,
            "misp_enabled": env_settings.misp_enabled,
            "misp_url": env_settings.misp_url,
            "misp_verify_ssl": env_settings.misp_verify_ssl,
            "slack_enabled": env_settings.slack_enabled,
            "slack_channel": env_settings.slack_channel,
            "slack_notify_on_escalation": env_settings.slack_notify_on_escalation,
            "slack_notify_on_verdict": env_settings.slack_notify_on_verdict,
            # LLM
            "llm_provider": env_llm_settings.llm_provider,
            "llm_fast_model": env_llm_settings.llm_fast_model,
            "llm_reasoning_model": env_llm_settings.llm_reasoning_model,
            "llm_temperature": env_llm_settings.llm_temperature,
            "llm_max_tokens": env_llm_settings.llm_max_tokens,
            "llm_anthropic_base_url": env_llm_settings.llm_anthropic_base_url,
            "llm_openai_base_url": env_llm_settings.llm_openai_base_url,
            "llm_openai_organization": env_llm_settings.llm_openai_organization,
        }

        for field, value in updates.items():
            if getattr(settings, field) != value:
                setattr(settings, field, value)
                changed = True

        if changed:
            settings.updated_at = datetime.utcnow()
            session.add(settings)
            await session.commit()
            await session.refresh(settings)

    return settings


async def fetch_integration_settings(session: AsyncSession) -> IntegrationSettings:
    """Fetch integration settings from the database.

    Args:
        session: Async database session.

    Returns:
        IntegrationSettings with values from database or defaults.
    """
    query = select(UserSettings).where(UserSettings.id == "default")
    result = await session.execute(query)
    db_settings = result.scalar_one_or_none()

    if db_settings is None:
        logger.info("no_settings_in_db_using_defaults")
        return IntegrationSettings()

    logger.info(
        "loaded_integration_settings",
        wazuh_enabled=db_settings.wazuh_enabled,
        cortex_enabled=db_settings.cortex_enabled,
        thehive_enabled=db_settings.thehive_enabled,
        misp_enabled=db_settings.misp_enabled,
        slack_enabled=db_settings.slack_enabled,
    )

    return IntegrationSettings(
        # Wazuh
        wazuh_enabled=db_settings.wazuh_enabled,
        wazuh_url=db_settings.wazuh_url,
        wazuh_verify_ssl=db_settings.wazuh_verify_ssl,
        # Cortex
        cortex_enabled=db_settings.cortex_enabled,
        cortex_url=db_settings.cortex_url,
        cortex_verify_ssl=db_settings.cortex_verify_ssl,
        # TheHive
        thehive_enabled=db_settings.thehive_enabled,
        thehive_url=db_settings.thehive_url,
        thehive_organisation=db_settings.thehive_organisation,
        thehive_verify_ssl=db_settings.thehive_verify_ssl,
        # MISP
        misp_enabled=db_settings.misp_enabled,
        misp_url=db_settings.misp_url,
        misp_verify_ssl=db_settings.misp_verify_ssl,
        # Slack
        slack_enabled=db_settings.slack_enabled,
        slack_channel=db_settings.slack_channel,
        slack_notify_on_escalation=db_settings.slack_notify_on_escalation,
        slack_notify_on_verdict=db_settings.slack_notify_on_verdict,
    )


async def fetch_llm_settings(session: AsyncSession) -> LLMSettings:
    """Fetch LLM settings from the database.

    Args:
        session: Async database session.

    Returns:
        LLMSettings with values from database or defaults.
    """
    query = select(UserSettings).where(UserSettings.id == "default")
    result = await session.execute(query)
    db_settings = result.scalar_one_or_none()

    if db_settings is None:
        logger.info("no_llm_settings_in_db_using_defaults")
        return LLMSettings()

    logger.info(
        "loaded_llm_settings",
        provider=db_settings.llm_provider,
        fast_model=db_settings.llm_fast_model,
        reasoning_model=db_settings.llm_reasoning_model,
    )

    return LLMSettings(
        llm_provider=db_settings.llm_provider if db_settings.llm_provider in ("anthropic", "openai") else "anthropic",
        llm_fast_model=db_settings.llm_fast_model,
        llm_reasoning_model=db_settings.llm_reasoning_model,
        llm_temperature=db_settings.llm_temperature,
        llm_max_tokens=db_settings.llm_max_tokens,
        llm_anthropic_base_url=db_settings.llm_anthropic_base_url,
        llm_openai_base_url=db_settings.llm_openai_base_url,
        llm_openai_organization=db_settings.llm_openai_organization,
    )


def create_wazuh_mcp_config(settings: IntegrationSettings) -> Optional[MCPServerConfig]:
    """Create Wazuh MCP server config from integration settings.

    Args:
        settings: Integration settings from database.

    Returns:
        MCPServerConfig if Wazuh is enabled and configured, None otherwise.
    """
    if not settings.wazuh_enabled:
        return None

    if not settings.wazuh_url:
        logger.warning("wazuh_enabled_but_url_missing")
        return None

    secrets = load_integration_secrets_from_env()
    if not secrets.wazuh_username or not secrets.wazuh_password:
        logger.warning(
            "wazuh_enabled_but_missing_credentials",
            username=bool(secrets.wazuh_username),
            password=bool(secrets.wazuh_password),
        )
        return None

    url = settings.wazuh_url
    if "://" not in url:
        url = f"https://{url}"

    parsed = urlsplit(url)
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 55000)

    base_path = Path(os.getenv("MCP_SERVERS_BASE_PATH", ".."))

    return MCPServerConfig(
        name="wazuh",
        path=Path(
            os.getenv(
                "WAZUH_MCP_SERVER_PATH",
                str(base_path / "mcp-server-wazuh" / "target" / "release" / "mcp-server-wazuh"),
            )
        ),
        env_vars={
            "WAZUH_API_HOST": host,
            "WAZUH_API_PORT": port,
            "WAZUH_API_USERNAME": secrets.wazuh_username,
            "WAZUH_API_PASSWORD": secrets.wazuh_password,
            "WAZUH_INDEXER_HOST": os.getenv("WAZUH_INDEXER_HOST", host),
            "WAZUH_INDEXER_PORT": os.getenv("WAZUH_INDEXER_PORT", "9200"),
            "WAZUH_INDEXER_USERNAME": os.getenv("WAZUH_INDEXER_USERNAME", "admin"),
            "WAZUH_INDEXER_PASSWORD": os.getenv("WAZUH_INDEXER_PASSWORD", "admin"),
            "WAZUH_VERIFY_SSL": "true" if settings.wazuh_verify_ssl else "false",
        },
    )


def create_cortex_mcp_config(settings: IntegrationSettings) -> Optional[MCPServerConfig]:
    """Create Cortex MCP server config from integration settings.

    Args:
        settings: Integration settings from database.

    Returns:
        MCPServerConfig if Cortex is enabled and configured, None otherwise.
    """
    if not settings.cortex_enabled:
        return None

    secrets = load_integration_secrets_from_env()

    if not settings.cortex_url or not secrets.cortex_api_key:
        logger.warning(
            "cortex_enabled_but_missing_config",
            url=bool(settings.cortex_url),
            api_key=bool(secrets.cortex_api_key),
        )
        return None

    base_path = Path(os.getenv("MCP_SERVERS_BASE_PATH", ".."))

    return MCPServerConfig(
        name="cortex",
        path=Path(
            os.getenv(
                "CORTEX_MCP_SERVER_PATH",
                str(base_path / "mcp-server-cortex" / "target" / "release" / "mcp-server-cortex"),
            )
        ),
        env_vars={
            "CORTEX_ENDPOINT": settings.cortex_url,
            "CORTEX_API_KEY": secrets.cortex_api_key,
            "CORTEX_VERIFY_SSL": "true" if settings.cortex_verify_ssl else "false",
        },
    )


def create_thehive_mcp_config(settings: IntegrationSettings) -> Optional[MCPServerConfig]:
    """Create TheHive MCP server config from integration settings.

    Args:
        settings: Integration settings from database.

    Returns:
        MCPServerConfig if TheHive is enabled and configured, None otherwise.
    """
    if not settings.thehive_enabled:
        return None

    secrets = load_integration_secrets_from_env()

    if not settings.thehive_url or not secrets.thehive_api_key:
        logger.warning(
            "thehive_enabled_but_missing_config",
            url=bool(settings.thehive_url),
            api_key=bool(secrets.thehive_api_key),
        )
        return None

    base_path = Path(os.getenv("MCP_SERVERS_BASE_PATH", ".."))

    env_vars = {
        "THEHIVE_URL": settings.thehive_url,
        "THEHIVE_API_TOKEN": secrets.thehive_api_key,
        "VERIFY_SSL": "true" if settings.thehive_verify_ssl else "false",
    }

    if settings.thehive_organisation:
        env_vars["THEHIVE_ORGANISATION"] = settings.thehive_organisation

    return MCPServerConfig(
        name="thehive",
        path=Path(
            os.getenv(
                "THEHIVE_MCP_SERVER_PATH",
                str(base_path / "mcp-server-thehive" / "target" / "release" / "mcp-server-thehive"),
            )
        ),
        env_vars=env_vars,
    )


def create_misp_mcp_config(settings: IntegrationSettings) -> Optional[MCPServerConfig]:
    """Create MISP MCP server config from integration settings.

    Args:
        settings: Integration settings from database.

    Returns:
        MCPServerConfig if MISP is enabled and configured, None otherwise.
    """
    if not settings.misp_enabled:
        return None

    secrets = load_integration_secrets_from_env()

    if not settings.misp_url or not secrets.misp_api_key:
        logger.warning(
            "misp_enabled_but_missing_config",
            url=bool(settings.misp_url),
            api_key=bool(secrets.misp_api_key),
        )
        return None

    base_path = Path(os.getenv("MCP_SERVERS_BASE_PATH", ".."))

    return MCPServerConfig(
        name="misp",
        path=Path(
            os.getenv(
                "MISP_MCP_SERVER_PATH",
                str(base_path / "mcp-server-misp" / "target" / "release" / "mcp-server-misp"),
            )
        ),
        env_vars={
            "MISP_URL": settings.misp_url,
            "MISP_API_KEY": secrets.misp_api_key,
            "MISP_VERIFY_SSL": "true" if settings.misp_verify_ssl else "false",
        },
    )


@dataclass
class EnabledMCPServers:
    """Container for enabled MCP server configurations."""

    wazuh: Optional[MCPServerConfig] = None
    cortex: Optional[MCPServerConfig] = None
    thehive: Optional[MCPServerConfig] = None
    misp: Optional[MCPServerConfig] = None

    @property
    def has_any_enabled(self) -> bool:
        """Check if any MCP server is enabled."""
        return any([self.wazuh, self.cortex, self.thehive, self.misp])

    @property
    def enabled_count(self) -> int:
        """Count of enabled MCP servers."""
        return sum(1 for s in [self.wazuh, self.cortex, self.thehive, self.misp] if s is not None)


def create_mcp_configs(settings: IntegrationSettings) -> EnabledMCPServers:
    """Create MCP server configurations based on integration settings.

    Args:
        settings: Integration settings from database.

    Returns:
        EnabledMCPServers with configs for enabled integrations.
    """
    return EnabledMCPServers(
        wazuh=create_wazuh_mcp_config(settings),
        cortex=create_cortex_mcp_config(settings),
        thehive=create_thehive_mcp_config(settings),
        misp=create_misp_mcp_config(settings),
    )
