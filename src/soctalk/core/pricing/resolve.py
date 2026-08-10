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

from soctalk.core.ir.policies import resolve_cost_tracking
from soctalk.core.pricing.names import base_model_id
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
# A gateway reselling somebody else's model is its OWN rate card: the price a
# tenant pays for ``deepseek-v4-flash`` through a reseller is the reseller's
# price, not DeepSeek's, and the two differ by up to 2.3x in our own
# measurements. So the slug names whoever is billing, which for a gateway is
# the gateway.
_HOST_VENDORS = {
    "api.deepseek.com": "deepseek",
    "novarouteai.com": "novaroute",
    "api.moonshot.cn": "moonshot",
    "api.mistral.ai": "mistral",
    "openrouter.ai": "openrouter",
    "dashscope.aliyuncs.com": "dashscope",
}


# Engines that mean the tokens are served on our own GPUs. Their marginal
# token cost is zero (the GPU hour is the real cost), which is why the catalog
# carries self_hosted rows at $0 rather than leaving them unpriced.
_SELF_HOSTED_ENGINES = frozenset({"vllm", "sglang"})

_HTTPX_DOT_FOLD = str.maketrans({
    "\u3002": ".",
    "\uff0e": ".",
    "\uff61": ".",
})


def authority_host_for_base_url(base_url: str | None) -> str:
    """Hostname normalized the same way httpx normalizes dot equivalents.

    ``httpx`` folds the IDNA dot characters U+3002, U+FF0E, and U+FF61 to
    ``.`` before connecting, but it does not decode ``%2e`` inside the
    authority. Keep that exact boundary so hosted-vendor checks agree with the
    actual request destination.
    """
    host = urlparse(base_url or "").hostname or ""
    return host.translate(_HTTPX_DOT_FOLD).lower()


def _provider_kind_from_authority(host: str) -> str | None:
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if host.endswith("api.openai.com"):
        return "openai"
    if host.endswith("api.anthropic.com"):
        return "anthropic"
    return None


def provider_kind_for(
    provider: str | None, base_url: str | None, engine: str | None = None
) -> str:
    """Classify a backend by protocol and, where the host says so, by vendor.

    ``openai-compatible`` is a protocol, not a provider: it identifies nothing
    on its own, and one gateway can route the same model string to different
    upstreams at different prices. So the kind records what we actually know,
    and ``provider_id_for`` carries the vendor when the host reveals it.

    ``engine`` wins when it names a self-hosted server on a non-hosted
    authority. A vLLM or SGLang tier speaks the OpenAI protocol on some
    arbitrary host, so without this it classified as ``openai_compatible`` and
    could never match the catalog's self_hosted rows — leaving a self-hosted
    tenant on the fail-expensive unknown-model fallback while a $0 row for its
    model sat in the table (Codex review round 2, finding 1). Hosted vendor
    authorities are checked first because the actual request still reaches
    that vendor even if a bad config also names a served engine.
    """
    p = (provider or "").strip().lower()
    if p == "anthropic":
        return "anthropic"
    host_kind = _provider_kind_from_authority(authority_host_for_base_url(base_url))
    if host_kind is not None:
        return host_kind
    if (engine or "").strip().lower() in _SELF_HOSTED_ENGINES:
        return "self_hosted"
    if p in ("openai", "openai-compatible"):
        return "openai_compatible"
    return "openai_compatible"


def provider_id_for(base_url: str | None) -> str | None:
    """The vendor slug a base URL identifies, or None when it does not."""
    host = authority_host_for_base_url(base_url)
    for known, slug in _HOST_VENDORS.items():
        if host == known or host.endswith("." + known):
            return slug
    return None


def override_key(provider_kind: str, provider_id: str | None, model: str) -> str:
    """The qualified key for a tenant price override.

    ``<provider_kind>:<provider_id or '*'>:<model>`` — the same triple the
    catalog is keyed on, so an override can say what a model costs *at a
    particular backend*.
    """
    return f"{provider_kind}:{provider_id or '*'}:{model}"


def _rates_from_override(
    overrides: dict[str, Any] | None,
    model: str,
    provider_kind: str | None = None,
    provider_id: str | None = None,
) -> dict[str, float] | None:
    """A tenant's own price for this model at this backend, if they set one.

    Two key shapes, most specific first:

    * ``<provider_kind>:<provider_id>:<model>`` — qualified (#141 phase 3).
    * ``<model>`` — the original shape, kept working.

    The original docstring claimed a model string was "well defined within one
    tenant because a tenant carries one provider and base URL". Per-tier
    backends broke that: a tenant can point its fast and reasoning roles at the
    same model string through different providers at different prices, and a
    model-keyed map silently collapsed the two into one (Codex review of #140).
    """
    if not overrides:
        return None
    entry: Any = None
    if provider_kind:
        entry = overrides.get(override_key(provider_kind, provider_id, model))
        if not isinstance(entry, dict) and provider_id:
            # A wildcard override covers every backend of this protocol.
            entry = overrides.get(override_key(provider_kind, None, model))
    if not isinstance(entry, dict):
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

    override = _rates_from_override(overrides, model, provider_kind, provider_id)
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


def roles_for_config(
    cfg: Any,
    tiers: dict[str, Any] | None = None,
    chat_model: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Each role with the backend it will ACTUALLY run on.

    Shared by run-time pricing and the config-time price gate. They used to
    derive this separately: the gate resolved one ``(kind, provider_id)`` from
    the primary config and applied it to every model, while runtime resolved
    each tier against its own provider and base URL. A tier could therefore
    pass the gate on the primary's catalog row and then run as ``unknown`` on
    its gateway, or be refused although it was priced (Codex review of phase 3).

    One function now, so the two cannot drift.

    A hybrid tenant (#12) can run fast and reasoning on different providers,
    and those cost different amounts even for the same model string. NULL or
    empty falls back to the primary, matching how render.py resolves
    fastModel / reasoningModel.
    """
    tiers = tiers or (getattr(cfg, "llm_tiers", None) or {})
    roles: dict[str, dict[str, Any]] = {}
    for role, fallback_model in (
        ("fast", cfg.llm_fast_model or cfg.llm_model),
        ("reasoning", cfg.llm_reasoning_model or cfg.llm_model),
        # Chat is a first-class role, not an afterthought. It calls
        # ``integ.llm_model`` (chat/agent.py), so a tenant with explicit fast
        # AND reasoning models runs chat on a model that appeared in NO role —
        # and therefore in no price snapshot. Its spend then fell through to
        # the built-in table or to `unknown`, even when the catalog priced it
        # perfectly well.
        #
        # Found by driving the config form on the NUC. Adding it here fixes the
        # pricing, and the config-time gate gets the same coverage for free
        # because both read this function.
        # ``chat_model`` overrides the tenant default when the CALLER knows
        # which model this conversation actually runs. A conversation can be
        # created with an explicit model (chat.py accepts body.model) and keeps
        # it as a tier override, so pricing the tenant default would stamp a
        # model the turn never calls (Codex, round 4 of the chat-role change).
        ("chat", chat_model or cfg.llm_model),
    ):
        tier = tiers.get(role) or {}
        provider = tier.get("provider") or cfg.llm_provider
        base_url = tier.get("base_url") or cfg.llm_base_url
        model = tier.get("model") or fallback_model
        # The primary config carries an engine too now (#142, Codex round 13):
        # without it a single-provider self-hosted tenant resolved by host and
        # stayed openai_compatible, missing its own self_hosted price. A tier's
        # engine still wins, since it describes that tier's own backend.
        engine = tier.get("engine") or getattr(cfg, "llm_engine", None)
        if not model:
            continue
        entry = {
            "model": model,
            "provider_kind": provider_kind_for(provider, base_url, engine),
            "provider_id": provider_id_for(base_url),
        }

        # CHAT only: never stamp a second entry for a model string that a
        # triage role already carries. _snapshot_rates matches by model across
        # roles, and two entries for one string with different rates are
        # treated as ambiguous and fall through to the built-in table — so
        # adding `chat` for a model `fast` already covers would push a run that
        # previously priced cleanly onto the fallback.
        #
        # fast and reasoning are deliberately NOT deduped. A hybrid tenant
        # running one model string through two providers genuinely has two
        # prices, and stamping both is what makes _snapshot_rates report the
        # ambiguity rather than silently pick one. Suppressing them collapsed
        # reasoning onto fast's backend — a worse bug than the one being fixed
        # (Codex, round 2 of the chat-role change).
        # Compared on the FAMILY id, not the exact string, because that is what
        # _snapshot_rates matches on. With an exact comparison, fast=gpt-4o and
        # chat=gpt-4o-2024-08-06 both got stamped and then both matched the same
        # lookup — reintroducing the ambiguity this skip exists to prevent
        # (Codex, round 3 of the chat-role change).
        if role == "chat" and any(
            base_model_id(existing["model"]) == base_model_id(model)
            for existing in roles.values()
        ):
            continue

        roles[role] = entry
    return roles


async def _cost_tracking_or_default(db: AsyncSession, tenant_id: UUID) -> bool:
    """The tenant's accounting switch, defaulting to ON if unreadable.

    Never fatal: an unreadable policy must not stop a run being created, and
    "on" is the safe side — it keeps ceilings enforced rather than silently
    switching them off.
    """
    try:
        return await resolve_cost_tracking(db, tenant_id)
    except Exception:  # noqa: BLE001
        return True


async def resolve_prices_for_backend(
    db: AsyncSession,
    *,
    model: str,
    provider: str | None,
    base_url: str | None,
    engine: str | None = None,
    overrides: dict[str, Any] | None = None,
    cost_tracking: bool = True,
) -> dict[str, Any] | None:
    """A snapshot for ONE model at an explicitly named backend (#142).

    For work that is not scoped to a tenant's LLM config — fleet chat runs the
    INSTALL's model on the install's backend, so pricing it from a focused
    tenant's config billed it against a backend it never touched, and with no
    focus it carried no snapshot at all and fell to the built-in table.

    Same snapshot shape as ``resolve_run_prices`` so every consumer
    (``_snapshot_rates``, ``_snapshot_source``, the provenance breakdown) works
    unchanged; it simply carries one role.
    """
    name = (model or "").strip()
    if not name:
        return None
    entry = await _resolve_one(
        db,
        model=name,
        provider_kind=provider_kind_for(provider, base_url, engine),
        provider_id=provider_id_for(base_url),
        overrides=overrides,
    )
    return {
        "version": SNAPSHOT_VERSION,
        "currency": "USD",
        "resolved_at": datetime.now(UTC).isoformat(),
        "cost_tracking": cost_tracking,
        "models": {"chat": entry},
    }


async def resolve_run_prices(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    integration_config_id: UUID | None = None,
    chat_model: str | None = None,
) -> dict[str, Any] | None:
    """The price snapshot to stamp on a new run, or None if unresolvable.

    ``integration_config_id`` is accepted now so that a tenant holding more
    than one LLM config later does not need this signature changed underneath
    its callers; today a tenant has one.

    Returns a snapshot with NO models when the tenant has no LLM config, rather
    than None. Pricing behaves identically — an empty ``models`` map matches
    nothing, so ``_snapshot_rates`` falls through to the legacy table exactly as
    an absent snapshot did — but the snapshot still carries ``cost_tracking``,
    which is what the worker reads to decide whether dollar ceilings apply at
    all. Returning None dropped that, so a tenant with accounting switched off
    was still halted on dollars for any run created before its LLM config
    existed (Codex round 8).
    """
    stmt = select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
    if integration_config_id is not None:
        stmt = stmt.where(IntegrationConfig.id == integration_config_id)
    cfg = (await db.execute(stmt)).scalars().first()
    if cfg is None:
        return {
            "version": SNAPSHOT_VERSION,
            "currency": "USD",
            "resolved_at": datetime.now(UTC).isoformat(),
            "cost_tracking": await _cost_tracking_or_default(db, tenant_id),
            "models": {},
        }

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
    roles = roles_for_config(cfg, tiers, chat_model=chat_model)

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
        # A config that resolves no roles (a blank primary model, which the LLM
        # PATCH permits) must still carry the switch, for the same reason as the
        # no-config case above (Codex round 9).
        return {
            "version": SNAPSHOT_VERSION,
            "currency": "USD",
            "resolved_at": datetime.now(UTC).isoformat(),
            "cost_tracking": await _cost_tracking_or_default(db, tenant_id),
            "models": {},
        }

    cost_tracking = await _cost_tracking_or_default(db, tenant_id)

    return {
        "version": SNAPSHOT_VERSION,
        "currency": "USD",
        "resolved_at": datetime.now(UTC).isoformat(),
        # Travels with the run so the worker enforces the policy that was in
        # force when the run started, without a database read per budget check.
        "cost_tracking": cost_tracking,
        "models": models,
    }
