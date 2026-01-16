"""User settings API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from soctalk.api.auth import UserIdentity, require_admin
from soctalk.api.deps import DbSession
from soctalk.persistence.models import UserSettings
from soctalk.settings_provider import (
    is_settings_readonly,
    load_integration_secrets_from_env,
    load_integration_settings_from_env,
    load_llm_secrets_from_env,
    load_llm_settings_from_env,
    seed_settings_from_env,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/settings", tags=["settings"])


ConfigSource = Literal["env", "db"]


# Request/Response models
class SettingsResponse(BaseModel):
    """User settings response model."""

    id: str
    readonly: bool

    # Field sources for effective values
    sources: dict[str, ConfigSource]

    # LLM settings (non-secret; keys are env-only)
    llm_provider: Literal["anthropic", "openai"]
    llm_fast_model: str
    llm_reasoning_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_anthropic_base_url: str | None
    llm_openai_base_url: str | None
    llm_openai_organization: str | None
    anthropic_api_key_configured: bool
    openai_api_key_configured: bool
    llm_keys_conflict: bool

    # Wazuh SIEM integration
    wazuh_enabled: bool
    wazuh_url: str | None
    wazuh_verify_ssl: bool
    wazuh_credentials_configured: bool

    # Cortex integration
    cortex_enabled: bool
    cortex_url: str | None
    cortex_verify_ssl: bool
    cortex_api_key_configured: bool

    # TheHive integration
    thehive_enabled: bool
    thehive_url: str | None
    thehive_organisation: str | None
    thehive_verify_ssl: bool
    thehive_api_key_configured: bool

    # MISP integration
    misp_enabled: bool
    misp_url: str | None
    misp_verify_ssl: bool
    misp_api_key_configured: bool

    # Slack integration
    slack_enabled: bool
    slack_channel: str | None
    slack_notify_on_escalation: bool
    slack_notify_on_verdict: bool
    slack_webhook_configured: bool

    updated_at: datetime


class SettingsUpdateRequest(BaseModel):
    """Request body for updating settings."""

    # LLM settings (non-secret; keys are env-only)
    llm_provider: Literal["anthropic", "openai"] | None = Field(None, description="LLM provider")
    llm_fast_model: str | None = Field(None, description="Fast model (routing/workers)")
    llm_reasoning_model: str | None = Field(None, description="Reasoning model (verdict)")
    llm_temperature: float | None = Field(None, ge=0.0, le=2.0, description="Sampling temperature")
    llm_max_tokens: int | None = Field(None, ge=1, le=200000, description="Max completion tokens")
    llm_anthropic_base_url: str | None = Field(None, description="Anthropic base URL (optional)")
    llm_openai_base_url: str | None = Field(None, description="OpenAI-compatible base URL (optional)")
    llm_openai_organization: str | None = Field(None, description="OpenAI organization (optional)")

    # Wazuh SIEM integration
    wazuh_enabled: bool | None = Field(None, description="Enable Wazuh SIEM integration")
    wazuh_url: str | None = Field(None, description="Wazuh API URL")
    wazuh_verify_ssl: bool | None = Field(None, description="Verify Wazuh SSL certificates")

    # Cortex integration
    cortex_enabled: bool | None = Field(None, description="Enable Cortex integration")
    cortex_url: str | None = Field(None, description="Cortex API URL")
    cortex_verify_ssl: bool | None = Field(None, description="Verify Cortex SSL certificates")

    # TheHive integration
    thehive_enabled: bool | None = Field(None, description="Enable TheHive integration")
    thehive_url: str | None = Field(None, description="TheHive API URL")
    thehive_organisation: str | None = Field(None, description="TheHive organisation")
    thehive_verify_ssl: bool | None = Field(None, description="Verify TheHive SSL certificates")

    # MISP integration
    misp_enabled: bool | None = Field(None, description="Enable MISP integration")
    misp_url: str | None = Field(None, description="MISP API URL")
    misp_verify_ssl: bool | None = Field(None, description="Verify MISP SSL certificates")

    # Slack integration
    slack_enabled: bool | None = Field(None, description="Enable Slack notifications")
    slack_channel: str | None = Field(None, description="Slack channel for notifications")
    slack_notify_on_escalation: bool | None = Field(None, description="Notify on escalation")
    slack_notify_on_verdict: bool | None = Field(None, description="Notify on verdict")


def _settings_to_response(settings: UserSettings, *, readonly: bool) -> SettingsResponse:
    """Convert settings + env to an effective response model."""
    env_settings = load_integration_settings_from_env()
    env_llm_settings = load_llm_settings_from_env()
    secrets = load_integration_secrets_from_env()
    llm_secrets = load_llm_secrets_from_env()

    def source(effective_value: object, env_value: object) -> ConfigSource:
        if readonly or effective_value == env_value:
            return "env"
        return "db"

    if readonly:
        effective = env_settings
        effective_llm = env_llm_settings
    else:
        effective = type(env_settings)(
            wazuh_enabled=settings.wazuh_enabled,
            wazuh_url=settings.wazuh_url or env_settings.wazuh_url,
            wazuh_verify_ssl=settings.wazuh_verify_ssl,
            cortex_enabled=settings.cortex_enabled,
            cortex_url=settings.cortex_url or env_settings.cortex_url,
            cortex_verify_ssl=settings.cortex_verify_ssl,
            thehive_enabled=settings.thehive_enabled,
            thehive_url=settings.thehive_url or env_settings.thehive_url,
            thehive_organisation=settings.thehive_organisation or env_settings.thehive_organisation,
            thehive_verify_ssl=settings.thehive_verify_ssl,
            misp_enabled=settings.misp_enabled,
            misp_url=settings.misp_url or env_settings.misp_url,
            misp_verify_ssl=settings.misp_verify_ssl,
            slack_enabled=settings.slack_enabled,
            slack_channel=settings.slack_channel or env_settings.slack_channel,
            slack_notify_on_escalation=settings.slack_notify_on_escalation,
            slack_notify_on_verdict=settings.slack_notify_on_verdict,
        )
        effective_llm = type(env_llm_settings)(
            llm_provider=settings.llm_provider if settings.llm_provider in ("anthropic", "openai") else env_llm_settings.llm_provider,  # type: ignore[arg-type]
            llm_fast_model=settings.llm_fast_model,
            llm_reasoning_model=settings.llm_reasoning_model,
            llm_temperature=settings.llm_temperature,
            llm_max_tokens=settings.llm_max_tokens,
            llm_anthropic_base_url=settings.llm_anthropic_base_url or env_llm_settings.llm_anthropic_base_url,
            llm_openai_base_url=settings.llm_openai_base_url or env_llm_settings.llm_openai_base_url,
            llm_openai_organization=settings.llm_openai_organization or env_llm_settings.llm_openai_organization,
        )

    sources: dict[str, ConfigSource] = {
        "llm_provider": source(effective_llm.llm_provider, env_llm_settings.llm_provider),
        "llm_fast_model": source(effective_llm.llm_fast_model, env_llm_settings.llm_fast_model),
        "llm_reasoning_model": source(effective_llm.llm_reasoning_model, env_llm_settings.llm_reasoning_model),
        "llm_temperature": source(effective_llm.llm_temperature, env_llm_settings.llm_temperature),
        "llm_max_tokens": source(effective_llm.llm_max_tokens, env_llm_settings.llm_max_tokens),
        "llm_anthropic_base_url": source(effective_llm.llm_anthropic_base_url, env_llm_settings.llm_anthropic_base_url),
        "llm_openai_base_url": source(effective_llm.llm_openai_base_url, env_llm_settings.llm_openai_base_url),
        "llm_openai_organization": source(
            effective_llm.llm_openai_organization,
            env_llm_settings.llm_openai_organization,
        ),
        "wazuh_enabled": source(effective.wazuh_enabled, env_settings.wazuh_enabled),
        "wazuh_url": source(effective.wazuh_url, env_settings.wazuh_url),
        "wazuh_verify_ssl": source(effective.wazuh_verify_ssl, env_settings.wazuh_verify_ssl),
        "cortex_enabled": source(effective.cortex_enabled, env_settings.cortex_enabled),
        "cortex_url": source(effective.cortex_url, env_settings.cortex_url),
        "cortex_verify_ssl": source(effective.cortex_verify_ssl, env_settings.cortex_verify_ssl),
        "thehive_enabled": source(effective.thehive_enabled, env_settings.thehive_enabled),
        "thehive_url": source(effective.thehive_url, env_settings.thehive_url),
        "thehive_organisation": source(effective.thehive_organisation, env_settings.thehive_organisation),
        "thehive_verify_ssl": source(effective.thehive_verify_ssl, env_settings.thehive_verify_ssl),
        "misp_enabled": source(effective.misp_enabled, env_settings.misp_enabled),
        "misp_url": source(effective.misp_url, env_settings.misp_url),
        "misp_verify_ssl": source(effective.misp_verify_ssl, env_settings.misp_verify_ssl),
        "slack_enabled": source(effective.slack_enabled, env_settings.slack_enabled),
        "slack_channel": source(effective.slack_channel, env_settings.slack_channel),
        "slack_notify_on_escalation": source(
            effective.slack_notify_on_escalation,
            env_settings.slack_notify_on_escalation,
        ),
        "slack_notify_on_verdict": source(
            effective.slack_notify_on_verdict,
            env_settings.slack_notify_on_verdict,
        ),
    }

    return SettingsResponse(
        id=settings.id,
        # Wazuh
        readonly=readonly,
        sources=sources,
        llm_provider=effective_llm.llm_provider,  # type: ignore[arg-type]
        llm_fast_model=effective_llm.llm_fast_model,
        llm_reasoning_model=effective_llm.llm_reasoning_model,
        llm_temperature=effective_llm.llm_temperature,
        llm_max_tokens=effective_llm.llm_max_tokens,
        llm_anthropic_base_url=effective_llm.llm_anthropic_base_url,
        llm_openai_base_url=effective_llm.llm_openai_base_url,
        llm_openai_organization=effective_llm.llm_openai_organization,
        anthropic_api_key_configured=bool(llm_secrets.anthropic_api_key),
        openai_api_key_configured=bool(llm_secrets.openai_api_key),
        llm_keys_conflict=bool(llm_secrets.anthropic_api_key and llm_secrets.openai_api_key),
        wazuh_enabled=effective.wazuh_enabled,
        wazuh_url=effective.wazuh_url,
        wazuh_verify_ssl=effective.wazuh_verify_ssl,
        wazuh_credentials_configured=bool(secrets.wazuh_username and secrets.wazuh_password),
        # Cortex
        cortex_enabled=effective.cortex_enabled,
        cortex_url=effective.cortex_url,
        cortex_verify_ssl=effective.cortex_verify_ssl,
        cortex_api_key_configured=bool(secrets.cortex_api_key),
        # TheHive
        thehive_enabled=effective.thehive_enabled,
        thehive_url=effective.thehive_url,
        thehive_organisation=effective.thehive_organisation,
        thehive_verify_ssl=effective.thehive_verify_ssl,
        thehive_api_key_configured=bool(secrets.thehive_api_key),
        # MISP
        misp_enabled=effective.misp_enabled,
        misp_url=effective.misp_url,
        misp_verify_ssl=effective.misp_verify_ssl,
        misp_api_key_configured=bool(secrets.misp_api_key),
        # Slack
        slack_enabled=effective.slack_enabled,
        slack_channel=effective.slack_channel,
        slack_notify_on_escalation=effective.slack_notify_on_escalation,
        slack_notify_on_verdict=effective.slack_notify_on_verdict,
        slack_webhook_configured=bool(secrets.slack_webhook_url),
        updated_at=settings.updated_at,
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(db: DbSession) -> SettingsResponse:
    """Get current user settings.

    Returns the settings for the default user. If no settings exist,
    creates default settings.

    Args:
        db: Database session.

    Returns:
        Current user settings.
    """
    readonly = is_settings_readonly()
    settings = await seed_settings_from_env(db, overwrite=readonly)

    return _settings_to_response(settings, readonly=readonly)


@router.put("", response_model=SettingsResponse)
async def update_settings(
    request: SettingsUpdateRequest,
    db: DbSession,
    _: UserIdentity | None = Depends(require_admin),
) -> SettingsResponse:
    """Update user settings.

    Only provided fields will be updated.

    Args:
        request: Settings update request.
        db: Database session.

    Returns:
        Updated user settings.
    """
    readonly = is_settings_readonly()
    if readonly:
        raise HTTPException(status_code=403, detail="Settings are read-only in this environment.")

    query = select(UserSettings).where(UserSettings.id == "default")
    result = await db.execute(query)
    settings = result.scalar_one_or_none()

    if settings is None:
        # Create settings with provided values
        settings = UserSettings(id="default")
        db.add(settings)

    # Update only provided fields
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings, field, value)

    secrets = load_integration_secrets_from_env()

    if settings.wazuh_enabled and not (secrets.wazuh_username and secrets.wazuh_password):
        raise HTTPException(
            status_code=400,
            detail="Wazuh is enabled but WAZUH_API_USER/WAZUH_API_PASSWORD are not set.",
        )
    if settings.cortex_enabled and not secrets.cortex_api_key:
        raise HTTPException(status_code=400, detail="Cortex is enabled but CORTEX_API_KEY is not set.")
    if settings.thehive_enabled and not secrets.thehive_api_key:
        raise HTTPException(
            status_code=400,
            detail="TheHive is enabled but THEHIVE_API_KEY (or THEHIVE_API_TOKEN) is not set.",
        )
    if settings.misp_enabled and not secrets.misp_api_key:
        raise HTTPException(status_code=400, detail="MISP is enabled but MISP_API_KEY is not set.")
    if settings.slack_enabled and not secrets.slack_webhook_url:
        raise HTTPException(status_code=400, detail="Slack is enabled but SLACK_WEBHOOK_URL is not set.")

    llm_secrets = load_llm_secrets_from_env()
    if llm_secrets.anthropic_api_key and llm_secrets.openai_api_key:
        raise HTTPException(
            status_code=400,
            detail="Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set. Choose exactly one LLM provider.",
        )
    if settings.llm_provider == "anthropic" and not llm_secrets.anthropic_api_key:
        raise HTTPException(status_code=400, detail="LLM provider is Anthropic but ANTHROPIC_API_KEY is not set.")
    if settings.llm_provider == "openai" and not llm_secrets.openai_api_key:
        raise HTTPException(status_code=400, detail="LLM provider is OpenAI but OPENAI_API_KEY is not set.")

    settings.updated_at = datetime.utcnow()
    db.add(settings)
    await db.commit()
    await db.refresh(settings)

    logger.info("settings_updated", settings_id="default", updates=list(update_data.keys()))

    return _settings_to_response(settings, readonly=readonly)


@router.post("/reset", response_model=SettingsResponse)
async def reset_settings(
    db: DbSession,
    _: UserIdentity | None = Depends(require_admin),
) -> SettingsResponse:
    """Reset settings to defaults.

    Args:
        db: Database session.

    Returns:
        Default settings.
    """
    readonly = is_settings_readonly()
    if readonly:
        raise HTTPException(status_code=403, detail="Settings are read-only in this environment.")

    query = select(UserSettings).where(UserSettings.id == "default")
    result = await db.execute(query)
    settings = result.scalar_one_or_none()

    if settings is not None:
        await db.delete(settings)

    env_settings = load_integration_settings_from_env()
    env_llm_settings = load_llm_settings_from_env()

    settings = UserSettings(
        id="default",
        llm_provider=env_llm_settings.llm_provider,
        llm_fast_model=env_llm_settings.llm_fast_model,
        llm_reasoning_model=env_llm_settings.llm_reasoning_model,
        llm_temperature=env_llm_settings.llm_temperature,
        llm_max_tokens=env_llm_settings.llm_max_tokens,
        llm_anthropic_base_url=env_llm_settings.llm_anthropic_base_url,
        llm_openai_base_url=env_llm_settings.llm_openai_base_url,
        llm_openai_organization=env_llm_settings.llm_openai_organization,
        wazuh_enabled=env_settings.wazuh_enabled,
        wazuh_url=env_settings.wazuh_url,
        wazuh_verify_ssl=env_settings.wazuh_verify_ssl,
        cortex_enabled=env_settings.cortex_enabled,
        cortex_url=env_settings.cortex_url,
        cortex_verify_ssl=env_settings.cortex_verify_ssl,
        thehive_enabled=env_settings.thehive_enabled,
        thehive_url=env_settings.thehive_url,
        thehive_organisation=env_settings.thehive_organisation,
        thehive_verify_ssl=env_settings.thehive_verify_ssl,
        misp_enabled=env_settings.misp_enabled,
        misp_url=env_settings.misp_url,
        misp_verify_ssl=env_settings.misp_verify_ssl,
        slack_enabled=env_settings.slack_enabled,
        slack_channel=env_settings.slack_channel,
        slack_notify_on_escalation=env_settings.slack_notify_on_escalation,
        slack_notify_on_verdict=env_settings.slack_notify_on_verdict,
    )
    db.add(settings)
    await db.commit()
    await db.refresh(settings)

    logger.info("settings_reset", settings_id="default")

    return _settings_to_response(settings, readonly=readonly)
