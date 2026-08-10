"""Configuration management for SocTalk agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from soctalk.core.llm_provider import has_usable_served_base_url

logger = structlog.get_logger(__name__)


class MCPServerConfig(BaseModel):
    """Configuration for an MCP server connection."""

    name: str
    path: Path
    env_vars: dict[str, str] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    """Configuration for LLM models."""

    provider: Literal["anthropic", "openai"] = "anthropic"
    fast_model: str = "claude-sonnet-4-6"
    reasoning_model: str = "claude-sonnet-4-6"
    chat_model: str = ""
    # Per-tier overlay for the InferenceRequest resolver (issue #32). Keyed by
    # InferenceTier value ('router'|'reasoning'|'chat'|'extraction'); each value
    # may set provider/engine/model/base_url/api_key/default_decoding_mode. Empty
    # means every tier falls back to the legacy fast_model/reasoning_model
    # defaults — the multi-provider chart (#4) populates this.
    tiers: dict[str, dict] = {}
    # Primary backend engine. Applies to tiers without an explicit
    # SOCTALK_<TIER>_ENGINE override, so a single-provider self-hosted tenant
    # runs and prices with the same engine classification.
    engine: Literal["frontier", "openai_compatible", "vllm", "sglang"] | None = None
    anthropic_api_key: str = ""
    anthropic_base_url: Optional[str] = None
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    openai_organization: Optional[str] = None
    # Router/supervisor tier sampling. Bounds mirror the API + chart schema so a
    # provisioning bypass or a hand-set env var can't boot the worker with a
    # negative temperature or an absurd token cap (Codex #3) — an out-of-range
    # value fails load_config() loudly at startup.
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=8192)
    # Chat-agent sampling (issue #10) — lifted out of the hardcoded literals in
    # chat/agent.py so the chat tier's params live alongside the triage params.
    chat_temperature: float = 0.2
    chat_max_tokens: int = 2048
    # Transport bounds passed to the provider SDK (single retry layer).
    timeout_seconds: float = 120.0
    max_retries: int = 2


class Config(BaseModel):
    """Main configuration for SocTalk agent."""

    # MCP Server paths
    wazuh_mcp_server: MCPServerConfig
    cortex_mcp_server: MCPServerConfig
    thehive_mcp_server: MCPServerConfig
    misp_mcp_server: MCPServerConfig

    # LLM settings
    llm: LLMConfig

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"


# Per-tier LLM env prefixes (issue #4). Each tier can override provider /
# base_url / engine / model / api_key / decoding mode independently, so the
# high-volume router loop can ride a cheap self-hosted (OpenAI-compatible)
# endpoint while the reasoning verdict stays on a frontier model. The existing
# single-provider vars remain the defaults for any tier left unset.
_TIER_ENV_PREFIXES: dict[str, str] = {
    "router": "SOCTALK_FAST",       # high-volume supervisor/router loop
    "reasoning": "SOCTALK_REASONING",
    "chat": "SOCTALK_CHAT",
    "extraction": "SOCTALK_EXTRACTION",
}
_TIER_ENV_FIELDS: dict[str, str] = {
    "PROVIDER": "provider",
    "MODEL": "model",
    "BASE_URL": "base_url",
    "API_KEY": "api_key",
    "ENGINE": "engine",
    "DECODING_MODE": "default_decoding_mode",
    "TEMPERATURE": "temperature",
    "MAX_TOKENS": "max_tokens",
}
# A tier entry is materialized only when one of these "routing-defining" fields
# is set — a bare ``SOCTALK_FAST_MODEL`` (the historical single-provider var)
# must NOT create a tier override, so existing deployments keep the strict
# single-provider behaviour and the mutual-exclusion guard.
_TIER_ROUTING_FIELDS = ("provider", "base_url", "engine")


def _load_tier_configs() -> dict[str, dict]:
    """Build the per-tier overlay for ``LLMConfig.tiers`` from env (issue #4).

    Returns a ``{tier: {field: value}}`` map the InferenceRequest resolver
    consumes. Only tiers that declare a provider/base_url/engine appear —
    a model-only setting flows through the legacy ``*_MODEL`` fields instead,
    so single-provider deployments produce an empty map (no behaviour change).
    """
    valid_providers = {"anthropic", "openai"}
    tiers: dict[str, dict] = {}
    for tier, prefix in _TIER_ENV_PREFIXES.items():
        entry: dict[str, str] = {}
        for suffix, field in _TIER_ENV_FIELDS.items():
            val = (os.getenv(f"{prefix}_{suffix}") or "").strip()
            if val:
                entry[field] = val
        if not any(entry.get(f) for f in _TIER_ROUTING_FIELDS):
            continue
        if "provider" in entry and entry["provider"] not in valid_providers:
            raise ValueError(
                f"Invalid {prefix}_PROVIDER={entry['provider']!r}. Expected 'anthropic' or 'openai'."
            )
        if "engine" in entry:
            from soctalk.inference import ProviderEngine
            try:
                engine = ProviderEngine(entry["engine"])
            except ValueError as e:
                raise ValueError(
                    f"Invalid {prefix}_ENGINE={entry['engine']!r}. "
                    f"Expected one of {[m.value for m in ProviderEngine]}."
                ) from e
            # A served / gateway engine has no public default endpoint — without a
            # base_url (per-tier OR the global OPENAI_BASE_URL the resolver
            # inherits) the worker would dial api.openai.com with a served model
            # name and fail. Fail loudly at config time instead.
            served = {ProviderEngine.VLLM, ProviderEngine.SGLANG,
                      ProviderEngine.OPENAI_COMPATIBLE}
            global_openai_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
            effective_base_url = entry.get("base_url") or global_openai_base
            if engine in served and not has_usable_served_base_url(effective_base_url):
                raise ValueError(
                    f"{prefix}_ENGINE={entry['engine']} requires {prefix}_BASE_URL "
                    "(or a custom global OPENAI_BASE_URL) — a served/gateway engine "
                    "cannot use the hosted OpenAI default endpoint."
                )
        if "default_decoding_mode" in entry:
            from soctalk.inference import DecodingMode
            try:
                DecodingMode(entry["default_decoding_mode"])
            except ValueError as e:
                raise ValueError(
                    f"Invalid {prefix}_DECODING_MODE={entry['default_decoding_mode']!r}. "
                    f"Expected one of {[m.value for m in DecodingMode]}."
                ) from e
        # Per-tier sampling overrides arrive as env strings — coerce + bound
        # them here (same 0–2 / 1–8192 range as the global knobs) so a bad value
        # fails config load loudly rather than reaching the provider SDK.
        if "temperature" in entry:
            try:
                temp = float(entry["temperature"])
            except ValueError as e:
                raise ValueError(
                    f"Invalid {prefix}_TEMPERATURE={entry['temperature']!r}. "
                    "Expected a number."
                ) from e
            if not 0.0 <= temp <= 2.0:
                raise ValueError(
                    f"{prefix}_TEMPERATURE={temp} out of range (0.0–2.0)."
                )
            entry["temperature"] = temp
        if "max_tokens" in entry:
            try:
                mt = int(entry["max_tokens"])
            except ValueError as e:
                raise ValueError(
                    f"Invalid {prefix}_MAX_TOKENS={entry['max_tokens']!r}. "
                    "Expected an integer."
                ) from e
            if not 1 <= mt <= 8192:
                raise ValueError(
                    f"{prefix}_MAX_TOKENS={mt} out of range (1–8192)."
                )
            entry["max_tokens"] = mt
        tiers[tier] = entry
    return tiers


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
    openai_base_url = _optional_env("OPENAI_BASE_URL") or _optional_env("OPENAI_API_BASE")

    provider_preference = (os.getenv("SOCTALK_LLM_PROVIDER") or "").strip().lower()
    if provider_preference and provider_preference not in {"anthropic", "openai"}:
        raise ValueError(
            f"Invalid SOCTALK_LLM_PROVIDER={provider_preference!r}. Expected 'anthropic' or 'openai'."
        )
    primary_engine = _optional_env("SOCTALK_LLM_ENGINE")
    primary_engine_is_served = False
    if primary_engine:
        from soctalk.inference import ProviderEngine
        try:
            parsed_engine = ProviderEngine(primary_engine)
        except ValueError as e:
            raise ValueError(
                f"Invalid SOCTALK_LLM_ENGINE={primary_engine!r}. "
                f"Expected one of {[m.value for m in ProviderEngine]}."
            ) from e
        served = {
            ProviderEngine.VLLM,
            ProviderEngine.SGLANG,
            ProviderEngine.OPENAI_COMPATIBLE,
        }
        primary_engine_is_served = parsed_engine in served

    # Per-tier provider overlay (issue #4). When present, the deployment has
    # opted into mixed-provider triage (e.g. self-hosted router + frontier
    # verdict), so both API keys may legitimately be set — the resolver scopes
    # each call to a single provider. Absent overrides keep the strict
    # single-provider guard.
    tier_configs = _load_tier_configs()

    # Relax the guard ONLY for genuine mixed-provider intent — a tier that names
    # a second provider or a served engine (vLLM/SGLang/openai-compatible, which
    # routes to the OpenAI client). A base_url-only override rides the global
    # provider and does NOT justify both keys, so it stays gated (a stray second
    # key there is a silent misconfig, not a mixed deployment).
    _served = {"vllm", "sglang", "openai_compatible"}
    has_mixed_intent = any(
        t.get("provider") or (t.get("engine") in _served)
        for t in tier_configs.values()
    )

    if anthropic_api_key and openai_api_key and not has_mixed_intent:
        raise ValueError(
            "Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set. "
            "SocTalk supports either Anthropic or an OpenAI-compatible provider (mutually exclusive) "
            "unless per-tier providers are configured (SOCTALK_<TIER>_PROVIDER / a served "
            "SOCTALK_<TIER>_ENGINE). Unset one of the keys."
        )

    if provider_preference:
        provider = provider_preference
    elif anthropic_api_key and openai_api_key:
        # Mixed mode with no explicit default — keep the historical default
        # (anthropic) as the global/fallback provider rather than silently
        # flipping on key presence.
        provider = "anthropic"
    else:
        provider = "openai" if openai_api_key else "anthropic"

    # Auto-correct provider if only the other provider's key is present
    # (single-provider convenience; skipped when both keys are set).
    if not (anthropic_api_key and openai_api_key):
        if provider == "anthropic" and not anthropic_api_key and openai_api_key:
            provider = "openai"
        if provider == "openai" and not openai_api_key and anthropic_api_key:
            provider = "anthropic"

    if primary_engine in {"openai_compatible", "vllm", "sglang"}:
        if provider == "anthropic":
            raise ValueError(
                f"SOCTALK_LLM_ENGINE={primary_engine!r} is OpenAI-compatible; "
                "not valid with SOCTALK_LLM_PROVIDER='anthropic'."
            )
        hosted_kind = None
        if openai_base_url:
            from soctalk.core.pricing.resolve import hosted_provider_kind_for_base_url

            hosted_kind = hosted_provider_kind_for_base_url(openai_base_url)
        if provider == "openai" and (hosted_kind is not None or not openai_base_url):
            logger.warning(
                "llm_engine_ignored_for_hosted_authority",
                engine=primary_engine,
                hosted_provider_kind=hosted_kind or "openai",
            )
            primary_engine = None
        elif primary_engine_is_served and not has_usable_served_base_url(openai_base_url):
            raise ValueError(
                f"SOCTALK_LLM_ENGINE={primary_engine} requires OPENAI_BASE_URL "
                "(or OPENAI_API_BASE) set to a custom endpoint — a served/gateway "
                "engine cannot use the hosted OpenAI default endpoint."
            )

    # The global-provider key is the fallback for tiers without their own
    # credential. When per-tier providers are configured (mixed intent, #4) the
    # tiers can supply their own keys — e.g. two self-hosted OpenAI-compatible
    # endpoints keyed only per-tier — so a missing global key is not fatal here;
    # create_chat_model still fails loudly per-call if a specific tier lacks one.
    if not has_mixed_intent:
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
        fast_model=os.getenv("SOCTALK_FAST_MODEL", "claude-sonnet-4-6"),
        reasoning_model=os.getenv("SOCTALK_REASONING_MODEL", "claude-sonnet-4-6"),
        chat_model=os.getenv("SOCTALK_CHAT_MODEL", ""),
        engine=primary_engine,  # type: ignore[arg-type]
        chat_temperature=float(os.getenv("SOCTALK_CHAT_TEMPERATURE", "0.2")),
        chat_max_tokens=int(os.getenv("SOCTALK_CHAT_MAX_TOKENS", "2048")),
        tiers=tier_configs,
        anthropic_api_key=anthropic_api_key,
        anthropic_base_url=_optional_env("ANTHROPIC_BASE_URL"),
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_organization=_optional_env("OPENAI_ORGANIZATION"),
        temperature=float(os.getenv("SOCTALK_LLM_TEMPERATURE", "0.0")),
        max_tokens=int(os.getenv("SOCTALK_LLM_MAX_TOKENS", "4096")),
        timeout_seconds=float(os.getenv("SOCTALK_LLM_TIMEOUT_SECONDS", "120")),
        max_retries=int(os.getenv("SOCTALK_LLM_MAX_RETRIES", "2")),
    )

    return Config(
        wazuh_mcp_server=wazuh_config,
        cortex_mcp_server=cortex_config,
        thehive_mcp_server=thehive_config,
        misp_mcp_server=misp_config,
        llm=llm_config,
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
