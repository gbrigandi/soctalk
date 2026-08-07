"""Turning a tenant's LLM config into the rates a run is priced at (#125).

Resolution happens once, when the run is created, and the answer is stamped on
the run row — the pattern #103 established for the token budget. Three things
follow from that and all three matter:

- the worker prices from the run rather than from its environment, so changing
  a price needs no rollout;
- an in-flight run cannot have its pricing shift underneath it;
- history is never re-priced by a later catalog edit, so "why was this run
  priced this way" keeps its answer.

The snapshot is keyed by ROLE (fast, reasoning), not by model string. A tenant
can point both roles at the same model through different providers, and those
cost different amounts; a model-keyed map would silently collapse them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.pricing import catalog
from soctalk.core.tenancy.models import IntegrationConfig

logger = structlog.get_logger(__name__)

SNAPSHOT_VERSION = 1

# Where a rate came from. Ordered by precedence, most authoritative first.
SOURCE_TENANT_OVERRIDE = "tenant_override"
SOURCE_PROVIDER_DECLARED = "provider_declared"
SOURCE_CATALOG = "catalog"
SOURCE_UNKNOWN = "unknown"

# Vendor slugs we can recognise from a base URL host, so a generic
# "openai-compatible" endpoint still gets attributed where the host makes it
# obvious. Deliberately small and literal: a guess that is wrong is worse than
# no guess, because it would price against another vendor's rate card.
_HOST_VENDORS = {
    "api.deepseek.com": "deepseek",
    "api.moonshot.cn": "moonshot",
    "api.mistral.ai": "mistral",
    "openrouter.ai": "openrouter",
    "dashscope.aliyuncs.com": "dashscope",
}


def provider_kind_for(provider: str | None, base_url: str | None) -> str:
    """Classify a backend by protocol and, where the host says so, by vendor.

    ``openai-compatible`` is a protocol, not a provider: it identifies nothing
    on its own, and one gateway can route the same model string to different
    upstreams at different prices. So the kind records what we actually know,
    and ``provider_id_for`` carries the vendor when the host reveals it.
    """
    p = (provider or "").strip().lower()
    if p == "anthropic":
        return "anthropic"
    host = (urlparse(base_url or "").hostname or "").lower()
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("api.openai.com"):
        return "openai"
    if p in ("openai", "openai-compatible"):
        return "openai_compatible"
    return "openai_compatible"


def provider_id_for(base_url: str | None) -> str | None:
    """The vendor slug a base URL identifies, or None when it does not."""
    host = (urlparse(base_url or "").hostname or "").lower()
    for known, slug in _HOST_VENDORS.items():
        if host == known or host.endswith("." + known):
            return slug
    return None


def _rates_from_override(
    overrides: dict[str, Any] | None, model: str
) -> dict[str, float] | None:
    """A tenant's own price for this model, if they set one (#121).

    The override map is keyed by model string alone, which is well defined
    within one tenant because a tenant carries one provider and base URL.
    """
    if not overrides:
        return None
    entry = overrides.get(model)
    if not isinstance(entry, dict):
        return None
    try:
        return {
            "input_per_mtok": float(entry["input"]),
            "output_per_mtok": float(entry["output"]),
        }
    except (KeyError, TypeError, ValueError):
        # A malformed override is not a reason to price the run wrongly in
        # silence; fall through to the catalog and say so.
        logger.warning("pricing_override_unreadable", model=model)
        return None


async def _resolve_one(
    db: AsyncSession,
    *,
    model: str,
    provider_kind: str,
    provider_id: str | None,
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Rates for one model, in precedence order, always with a source."""
    entry: dict[str, Any] = {
        "model": model,
        "provider_kind": provider_kind,
        "provider_id": provider_id,
    }

    override = _rates_from_override(overrides, model)
    if override is not None:
        entry.update(override)
        entry["source"] = SOURCE_TENANT_OVERRIDE
        return entry

    row = await catalog.lookup(
        db, provider_kind=provider_kind, model=model, provider_id=provider_id
    )
    if row is not None:
        dims = row.dimensions or {}
        entry["input_per_mtok"] = catalog.dollars_per_mtok(
            dims, "input_per_mtok_microusd"
        )
        entry["output_per_mtok"] = catalog.dollars_per_mtok(
            dims, "output_per_mtok_microusd"
        )
        for key, out_key in (
            ("cache_read_per_mtok_microusd", "cache_read_per_mtok"),
            ("cache_write_per_mtok_microusd", "cache_write_per_mtok"),
        ):
            value = catalog.dollars_per_mtok(dims, key)
            if value is not None:
                entry[out_key] = value
        entry["source"] = SOURCE_CATALOG
        entry["as_of"] = row.as_of.isoformat() if row.as_of else None
        return entry

    # Nothing knew this model. Say so explicitly rather than leaving the field
    # absent: a stamped "unknown" is distinguishable from an older run that was
    # never stamped at all, and #124 turns on being able to tell them apart.
    entry["source"] = SOURCE_UNKNOWN
    return entry


async def resolve_run_prices(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    integration_config_id: UUID | None = None,
) -> dict[str, Any] | None:
    """The price snapshot to stamp on a new run, or None if unresolvable.

    ``integration_config_id`` is accepted now so that a tenant holding more
    than one LLM config later does not need this signature changed underneath
    its callers; today a tenant has one.

    Returns None only when the tenant has no LLM config at all, which leaves
    the run unstamped and the legacy pricing path in charge — the behaviour an
    install gets before anything is configured.
    """
    stmt = select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
    if integration_config_id is not None:
        stmt = stmt.where(IntegrationConfig.id == integration_config_id)
    cfg = (await db.execute(stmt)).scalars().first()
    if cfg is None:
        return None

    overrides = getattr(cfg, "llm_model_prices", None)
    tiers = cfg.llm_tiers or {}

    # Each role is resolved against ITS OWN backend. A hybrid tenant (#12) can
    # run the fast tier on one provider and the reasoning tier on another, and
    # those cost different amounts even for the same model string — pricing
    # both against the primary config would silently bill one of them at the
    # other's rate (Codex review, finding 2).
    #
    # NULL or empty falls back to the primary, matching how render.py resolves
    # fastModel / reasoningModel.
    roles: dict[str, dict[str, Any]] = {}
    for role, fallback_model in (
        ("fast", cfg.llm_fast_model or cfg.llm_model),
        ("reasoning", cfg.llm_reasoning_model or cfg.llm_model),
    ):
        tier = tiers.get(role) or {}
        provider = tier.get("provider") or cfg.llm_provider
        base_url = tier.get("base_url") or cfg.llm_base_url
        model = tier.get("model") or fallback_model
        if not model:
            continue
        roles[role] = {
            "model": model,
            "provider_kind": provider_kind_for(provider, base_url),
            "provider_id": provider_id_for(base_url),
        }

    models: dict[str, Any] = {}
    for role, spec in roles.items():
        models[role] = await _resolve_one(
            db,
            model=spec["model"],
            provider_kind=spec["provider_kind"],
            provider_id=spec["provider_id"],
            overrides=overrides,
        )

    if not models:
        return None

    return {
        "version": SNAPSHOT_VERSION,
        "currency": "USD",
        "resolved_at": datetime.now(UTC).isoformat(),
        "models": models,
    }
