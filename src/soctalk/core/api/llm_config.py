"""Per-tenant LLM config endpoints.

MSSP admins set the LLM endpoint, model, and upload the API key. The key
lands in two places:

1. ``IntegrationConfig.llm_api_key_plain`` Postgres column — AUTHORITATIVE.
   The install-spec builder reads from here when producing install
   values; the soctalk-tenant chart templates its own Secret on L2
   from the plaintext value passed through.
2. K8s Secret ``tenant-<id>-llm`` in ``soctalk-system`` — legacy
   in-cluster (collapsed-tier) path, where the adapter / tenant chart
   mount the Secret directly by reference. Written alongside on every
   update for operators still running collapsed-tier, but NOT read
   back for ``has_api_key`` — a post-clear lingering Secret must not
   resurrect the flag.

DELETE clears the column first (authoritative), then deletes the Secret
best-effort. A failed / skipped Secret delete leaves the Secret on
disk but ``has_api_key`` reports false regardless, matching the
install-spec behavior (empty ``values.llm.apiKey`` → chart skips the
Secret template).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.llm_provider import normalize_provider
from soctalk.core.provisioning.k8s import new_k8s_client
from soctalk.core.tenancy.auth import current_identity
from soctalk.core.tenancy.context import tenant_context
from soctalk.core.tenancy.decorators import require_role, require_tenant_role
from soctalk.core.tenancy.models import IntegrationConfig, Role

router = APIRouter(prefix="/api/mssp/tenants", tags=["mssp-tenant-llm"])

# Tenant-side BYOK router. Tenant admins paste their own LLM API key
# instead of consuming the MSSP's shared install key. The PUT/DELETE
# endpoints exercise the same dual-write helpers as the MSSP path
# (Postgres-authoritative + K8s Secret), but tenant scope is implicit
# from the session identity — no path parameter — so a tenant_admin
# can't mutate another tenant's config even with a forged ID.
tenant_router = APIRouter(prefix="/api/tenant/llm", tags=["tenant-llm"])


class LlmConfigRead(BaseModel):
    provider: str
    base_url: str
    model: str
    # Per-role model overrides. ``None`` means "no override — falls
    # back to ``model``" (render.py resolves override-or-llm_model
    # into runsWorker.fastModel / reasoningModel).
    fast_model: str | None = None
    reasoning_model: str | None = None
    # Tenant-global default sampling for the router/supervisor tier. Rendered
    # into SOCTALK_LLM_TEMPERATURE / SOCTALK_LLM_MAX_TOKENS worker env.
    temperature: float
    max_tokens: int
    # Per-tenant case-run budget caps (issue #5). ``None`` = the worker default
    # ($5 / 15k). Rendered into SOCTALK_CASE_RUN_*_BUDGET; enforced in budget.py.
    dollar_budget_per_run: float | None = None
    token_budget_per_run: int | None = None
    has_api_key: bool
    # ``customer_safe`` mode for tenant-side rendering: shows the
    # last 4 chars of the configured key so the tenant can sanity-
    # check WHICH key is in use, without leaking the secret. Empty
    # string when no key is set.
    api_key_preview: str = ""
    # Per-tier LLM backends for a hybrid tenant (issue #12). Sanitized —
    # ``has_api_key`` per tier, never the plaintext. None = single-provider.
    tiers: dict[str, Any] | None = None


def _cross_provider_tiers_without_key(
    primary_provider: str | None, llm_tiers: dict[str, Any] | None
) -> list[str]:
    """Tier names on a DIFFERENT provider than the primary that carry no own key.

    A provisioned tenant mounts only the primary provider's credential, so such a
    tier can't authenticate and the worker would CrashLoop at startup — the API
    rejects it (422) at config time instead. openai/openai-compatible collapse to
    one runtime provider.
    """
    def _canon(p: str | None) -> str:
        return "openai" if p in ("openai", "openai-compatible") else (p or "")

    primary = _canon(primary_provider)
    return [
        tier for tier, block in (llm_tiers or {}).items()
        if _canon((block or {}).get("provider")) != primary and not (block or {}).get("api_key_plain")
    ]


def _merge_tier_keys(
    prior: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile per-tier keys across a sanitized round-trip.

    The GET view strips ``api_key_plain`` (only ``has_api_key`` survives), so a
    UI that reads-edits-writes the tiers map cannot echo the key back. Without
    reconciliation, a plain replace would silently drop every tier's key on the
    first model-only edit. Per-tier semantics on the incoming block:

    - ``api_key_plain`` **absent** → KEEP the stored key (carry it forward);
    - ``api_key_plain`` **empty / whitespace** → CLEAR (drop the own key, tier
      falls back to the primary credential);
    - ``api_key_plain`` **non-empty** → REPLACE verbatim.

    Only keys are reconciled; every other field is taken from ``incoming`` as-is
    (a genuine replace). ``prior`` is the stored (validated) tiers map.
    """
    prior = prior or {}
    merged: dict[str, Any] = {}
    for tier, block in incoming.items():
        block = dict(block or {})
        if "api_key_plain" not in block:
            existing = (prior.get(tier) or {}).get("api_key_plain")
            if existing:
                block["api_key_plain"] = existing
        elif not str(block.get("api_key_plain") or "").strip():
            # Explicit clear — drop the sentinel so validate_llm_tiers doesn't
            # store an empty string (which would read back as has_api_key=false
            # anyway, but keep the column clean).
            block.pop("api_key_plain", None)
        merged[tier] = block
    return merged


def _sanitize_tiers(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read-safe view of ``llm_tiers`` — strips per-tier plaintext keys."""
    if not raw:
        return None
    out: dict[str, Any] = {}
    for tier, block in raw.items():
        block = block or {}
        out[tier] = {
            "provider": block.get("provider"),
            "base_url": block.get("base_url"),
            "model": block.get("model"),
            "engine": block.get("engine"),
            "decoding_mode": block.get("decoding_mode"),
            "temperature": block.get("temperature"),
            "max_tokens": block.get("max_tokens"),
            "has_api_key": bool(block.get("api_key_plain")),
        }
    return out


def _mask_key(api_key: str | None) -> str:
    """Return ``"sk-…ABCD"`` style preview, or empty string if absent.

    Shows enough to disambiguate (last 4 chars after the provider
    prefix when one is detectable) without leaking the secret.
    A constant-length redaction would defeat the point of having a
    preview at all.
    """
    if not api_key:
        return ""
    s = api_key.strip()
    if len(s) < 8:
        return "…" + s[-2:]
    # Common provider prefixes — keep them so the user knows which
    # vendor key is loaded. Falls back to a generic ``…last4`` mask.
    for prefix in ("sk-ant-", "sk-proj-", "sk-"):
        if s.startswith(prefix):
            return f"{prefix}…{s[-4:]}"
    return f"…{s[-4:]}"


class LlmConfigUpdate(BaseModel):
    """Changed-fields-only PATCH payload.

    ``fast_model`` / ``reasoning_model`` are tri-state:

    - ``None`` / omitted → leave the stored override unchanged;
    - empty or whitespace-only string → CLEAR the override to NULL
      (revert to the ``llm_model`` fallback at render time);
    - any other value → set verbatim.

    The empty-string-clears convention exists because the UI panel
    must let an operator revert to "use the primary model" — ``None``
    can't express that in a changed-fields-only PATCH.
    """

    # ``openai`` and ``anthropic`` are the values the runs-worker's
    # ``load_config()`` actually accepts (see soctalk/config.py). The
    # chart maps the install-side ``openai-compatible`` enum to
    # ``openai`` at render time, so all three are valid inputs here.
    # The previous regex (only ``openai-compatible``) silently blocked
    # the very transitions an MSSP admin needs — switching a tenant to
    # Anthropic returned 422 with no useful message.
    provider: str | None = Field(
        default=None,
        pattern="^(openai|anthropic|openai-compatible)$",
    )
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=255)
    # No min_length — the empty string is a meaningful value (CLEAR),
    # see the class docstring for the tri-state contract.
    fast_model: str | None = Field(default=None, max_length=255)
    reasoning_model: str | None = Field(default=None, max_length=255)
    # Tenant-global default sampling. ``None`` = unchanged. Bounds mirror the
    # chart's values.schema.json + LLMConfig so an out-of-range value is rejected
    # here rather than failing Helm validation / worker startup on the next
    # re-render. ``max_tokens`` is a ROUTER output cap (Codex #1): a schema-
    # constrained decision is tiny, so 8192 is generous headroom — a future
    # large-generation setting would be a separate field, not this ceiling.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    # Per-tenant case-run budget caps (issue #5). Nullable columns, so these are
    # true tri-state via ``model_fields_set``: field ABSENT = unchanged; field
    # present as ``null`` = clear to the worker default; a number = set. Bounds:
    # a $0.10 floor (not just > 0) so a sub-cent cap can't halt the run right
    # after the first priced call (Codex); tokens ≥ 1000.
    dollar_budget_per_run: float | None = Field(default=None, ge=0.10, le=10000)
    token_budget_per_run: int | None = Field(default=None, ge=1000, le=100_000_000)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    # Per-tier LLM backends for a hybrid tenant (issue #12). ``None`` = leave
    # unchanged; ``{}`` = clear back to single-provider; a map = replace.
    # Shape ``{"fast": {provider, base_url, model, engine?, api_key_plain?}, ...}``
    # validated server-side by ``validate_llm_tiers`` (422 on a bad block).
    tiers: dict[str, Any] | None = Field(default=None)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, v):
        # Storage canonicalizes ``openai`` → ``openai-compatible`` via the
        # shared helper (single source of truth, also used by the onboard
        # wizard's TenantOnboard) — see soctalk.core.llm_provider for the
        # chart-schema rationale.
        return normalize_provider(v)

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, v):
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http(s)://")
        return v

    @field_validator(
        "temperature", "max_tokens", "dollar_budget_per_run",
        "token_budget_per_run", mode="before",
    )
    @classmethod
    def _reject_bool(cls, v):
        # Pydantic v2 coerces ``bool`` → 0/1 for numeric fields; ``True`` would
        # silently become temperature 1.0 / max_tokens 1. A config API must not
        # accept a boolean where a number is meant (Codex #4).
        if isinstance(v, bool):
            raise ValueError("must be a number, not a boolean")
        return v


def _db(request: Request) -> AsyncSession:
    session = getattr(request.state, "db", None)
    if session is None:
        raise HTTPException(500, "db session not attached to request")
    return session


def _soctalk_system_ns() -> str:
    return os.getenv("SOCTALK_SYSTEM_NS", "soctalk-system")


@router.get(
    "/{tenant_id}/llm",
    response_model=LlmConfigRead,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))],
)
async def get_tenant_llm(tenant_id: UUID, request: Request) -> LlmConfigRead:
    session = _db(request)
    async with tenant_context(session, tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
        )).scalar_one_or_none()
    if cfg is None:
        # Return a read-only default view when the tenant hasn't been
        # configured yet rather than 404 — the UI expects sensible defaults.
        default_cfg = IntegrationConfig(tenant_id=tenant_id)
        cfg = default_cfg
    # Postgres column is the sole source of truth for has_api_key.
    # A lingering K8s Secret (e.g. clear happened but the best-effort
    # delete was skipped) must NOT resurrect the flag — the install
    # spec builder reads ``integration.llm_api_key_plain`` and emits
    # an empty ``values.llm.apiKey`` when cleared, so GET and the
    # install contract stay aligned.
    return LlmConfigRead(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        fast_model=cfg.llm_fast_model,
        reasoning_model=cfg.llm_reasoning_model,
        temperature=cfg.llm_temperature,
        max_tokens=cfg.llm_max_tokens,
        dollar_budget_per_run=cfg.llm_dollar_budget_per_run,
        token_budget_per_run=cfg.llm_token_budget_per_run,
        has_api_key=bool(cfg.llm_api_key_plain),
        api_key_preview=_mask_key(cfg.llm_api_key_plain),
        tiers=_sanitize_tiers(cfg.llm_tiers),
    )


@router.patch(
    "/{tenant_id}/llm",
    response_model=LlmConfigRead,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))],
)
async def update_tenant_llm(
    tenant_id: UUID, payload: LlmConfigUpdate, request: Request
) -> LlmConfigRead:
    session = _db(request)
    # Resolve the tenant slug — the runs-worker mounts the LLM Secret
    # in ``tenant-<slug>`` and we need to write there in addition to the
    # legacy ``soctalk-system/tenant-<id>-llm`` audit copy. The state is
    # needed below to pick the right provisioning-job kind for
    # chart-affecting changes.
    from soctalk.core.tenancy.models import Tenant as _Tenant
    from soctalk.core.tenancy.models import TenantState

    tenant_row = (
        await session.execute(
            select(_Tenant.slug, _Tenant.state).where(_Tenant.id == tenant_id)
        )
    ).one_or_none()
    tenant_slug: str | None = tenant_row.slug if tenant_row else None
    tenant_state: str | None = tenant_row.state if tenant_row else None
    async with tenant_context(session, tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(IntegrationConfig.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if cfg is None:
            # Initialize a default row so subsequent renders / provisioning
            # operate against a concrete IntegrationConfig instead of 404.
            cfg = IntegrationConfig(tenant_id=tenant_id)
            session.add(cfg)
            await session.flush()
        # Heal historical rows that may have stored ``openai`` directly
        # before the LlmConfigUpdate normalizer landed. Without this,
        # a PATCH that doesn't touch ``provider`` (e.g. a model-only
        # change) would leave ``cfg.llm_provider='openai'`` in place
        # and the subsequent render emits ``values.llm.provider=openai``
        # — a value the tenant chart's values.schema.json rejects.
        # Idempotent: a no-op for rows already on a valid value.
        if cfg.llm_provider == "openai":
            cfg.llm_provider = "openai-compatible"
        # Snapshot the chart-affecting fields BEFORE mutation so we can
        # decide whether to enqueue a re-provision below. Provider /
        # base_url / model all flow through render_tenant_values into
        # values.llm.* (and from there into the runs-worker's
        # SOCTALK_LLM_PROVIDER + provider-specific API-key env-var
        # name). They cannot be propagated by a Secret rewrite + pod
        # restart alone — the env-var schema is fixed at chart-render
        # time. Without a re-render, a tenant switching provider stays
        # stuck on the old one until a manual helm upgrade.
        prior_provider = cfg.llm_provider
        prior_base_url = cfg.llm_base_url
        prior_model = cfg.llm_model
        prior_fast_model = cfg.llm_fast_model
        prior_reasoning_model = cfg.llm_reasoning_model
        prior_temperature = cfg.llm_temperature
        prior_max_tokens = cfg.llm_max_tokens
        prior_dollar_budget = cfg.llm_dollar_budget_per_run
        prior_token_budget = cfg.llm_token_budget_per_run
        prior_tiers = cfg.llm_tiers
        if payload.provider is not None:
            cfg.llm_provider = payload.provider
        if payload.base_url is not None:
            cfg.llm_base_url = payload.base_url
        if payload.model is not None:
            cfg.llm_model = payload.model
        # Tri-state override semantics (see LlmConfigUpdate docstring):
        # None = unchanged; ''/whitespace = clear to NULL (revert to the
        # llm_model fallback); anything else = set verbatim.
        if payload.fast_model is not None:
            cfg.llm_fast_model = payload.fast_model.strip() or None
        if payload.reasoning_model is not None:
            cfg.llm_reasoning_model = payload.reasoning_model.strip() or None
        # Tenant-global default sampling (None = unchanged). Bounds validated on
        # the payload model; render.py emits them to the worker env.
        if payload.temperature is not None:
            cfg.llm_temperature = payload.temperature
        if payload.max_tokens is not None:
            cfg.llm_max_tokens = payload.max_tokens
        # Budget caps are nullable, so use model_fields_set to tell "unchanged"
        # (field absent) from "clear to the default" (field sent as null).
        fields_set = payload.model_fields_set
        if "dollar_budget_per_run" in fields_set:
            cfg.llm_dollar_budget_per_run = payload.dollar_budget_per_run
        if "token_budget_per_run" in fields_set:
            cfg.llm_token_budget_per_run = payload.token_budget_per_run
        # Per-tier backends (issue #12): None = unchanged; {} = clear to
        # single-provider (NULL); a map = validate + replace. Replacement
        # assignment (not in-place) so the JSONB column is marked dirty.
        if payload.tiers is not None:
            from soctalk.core.tenancy.models import validate_llm_tiers
            # Carry forward per-tier keys the sanitized GET view stripped, so a
            # model-only edit doesn't silently drop them (keep/replace/clear).
            merged_tiers = _merge_tier_keys(prior_tiers, payload.tiers)
            try:
                cfg.llm_tiers = validate_llm_tiers(merged_tiers)
            except ValueError as e:
                raise HTTPException(422, f"invalid llm_tiers: {e}") from e
        # A tier on a DIFFERENT provider than the tenant's primary must carry its
        # own key (the tenant mounts only the primary credential). Re-check
        # whenever the primary provider OR the tiers changed — NOT only when
        # ``tiers`` is in the payload: a provider-only PATCH can turn an existing
        # same-provider tier (reusing the primary key) into a cross-provider tier
        # with no key, which would CrashLoop the worker. Runs against the final
        # merged state so keep/replace/clear are already resolved.
        if cfg.llm_provider != prior_provider or cfg.llm_tiers != prior_tiers:
            offending = _cross_provider_tiers_without_key(cfg.llm_provider, cfg.llm_tiers)
            if offending:
                raise HTTPException(
                    422,
                    f"llm_tiers {offending} use a different provider than the tenant's "
                    f"{cfg.llm_provider!r} but supply no api_key_plain; a cross-provider "
                    "tier must supply its own key.",
                )
        # Overrides are chart-affecting: render.py resolves them into
        # runsWorker.fastModel / reasoningModel, so a change (including
        # a clear) needs a helm re-render exactly like provider/model.
        chart_affecting_changed = (
            cfg.llm_provider != prior_provider
            or cfg.llm_base_url != prior_base_url
            or cfg.llm_model != prior_model
            or cfg.llm_fast_model != prior_fast_model
            or cfg.llm_reasoning_model != prior_reasoning_model
            or cfg.llm_temperature != prior_temperature
            or cfg.llm_max_tokens != prior_max_tokens
            or cfg.llm_dollar_budget_per_run != prior_dollar_budget
            or cfg.llm_token_budget_per_run != prior_token_budget
            or cfg.llm_tiers != prior_tiers
        )
        if chart_affecting_changed:
            # Enqueue a provisioning job so the worker helm-upgrades the
            # release with the new values from integration_configs.
            #
            # Kind selection is state-aware: for an ACTIVE tenant we must
            # enqueue ``tenant.reconcile`` — ``provision()`` early-returns
            # on active and active→provisioning is illegal per the
            # transition table, so a tenant.provision job would silently
            # never re-render the release (stale env schema + stale
            # LLM-host FQDN egress allow-list). Every other state keeps
            # the existing ``tenant.provision`` path (e.g. degraded →
            # provisioning is the legal retry route).
            #
            # The partial unique index ``uq_provisioning_jobs_active``
            # rejects a second pending/in_flight row for the same
            # (tenant, kind) — pre-check for the kind we are about to
            # enqueue rather than catch IntegrityError so the PATCH
            # transaction doesn't get poisoned. When an active job
            # already exists, that job's eventual run will read the
            # latest integration_configs row, so the LLM-config change
            # still propagates.
            from soctalk.core.tenancy.models import ProvisioningJob

            job_kind = (
                "tenant.reconcile"
                if tenant_state == TenantState.ACTIVE.value
                else "tenant.provision"
            )
            existing_active = (
                await session.execute(
                    select(ProvisioningJob).where(
                        ProvisioningJob.tenant_id == tenant_id,
                        ProvisioningJob.kind == job_kind,
                        ProvisioningJob.status.in_(["pending", "in_flight"]),
                    )
                )
            ).scalar_one_or_none()
            if existing_active is None:
                session.add(
                    ProvisioningJob(
                        tenant_id=tenant_id,
                        kind=job_kind,
                        status="pending",
                    )
                )
        await session.flush()

    if payload.api_key is not None:
        # Dual-write: Postgres (cross-cluster) + K8s Secret (legacy
        # in-cluster). A K8s write failure shouldn't orphan the
        # Postgres update; log and continue. Admins running a pure
        # cross-cluster deploy won't have a reachable soctalk-system
        # namespace and should expect the K8s write to fail silently.
        #
        # Transactional ordering: commit the Postgres row FIRST, then
        # trigger the K8s side effect. The DB-session middleware would
        # otherwise commit after the handler returns, which would
        # leave the K8s Secret written when a late commit failure
        # rolls back the Postgres column — violating the
        # "Postgres-is-authoritative" contract.
        async with tenant_context(session, tenant_id):
            cfg.llm_api_key_plain = payload.api_key
            await session.flush()
        await session.commit()
        try:
            await _write_api_key(tenant_id, payload.api_key, tenant_slug)
        except Exception as exc:  # pragma: no cover — best-effort path
            import structlog
            structlog.get_logger().warning(
                "llm_api_key_k8s_write_failed",
                tenant_id=str(tenant_id),
                error=str(exc),
            )

    # Same Postgres-authoritative contract as GET — see get_tenant_llm.
    return LlmConfigRead(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        fast_model=cfg.llm_fast_model,
        reasoning_model=cfg.llm_reasoning_model,
        temperature=cfg.llm_temperature,
        max_tokens=cfg.llm_max_tokens,
        dollar_budget_per_run=cfg.llm_dollar_budget_per_run,
        token_budget_per_run=cfg.llm_token_budget_per_run,
        has_api_key=bool(cfg.llm_api_key_plain),
        api_key_preview=_mask_key(cfg.llm_api_key_plain),
        tiers=_sanitize_tiers(cfg.llm_tiers),
    )


async def _write_api_key(tenant_id: UUID, api_key: str, tenant_slug: str | None = None) -> None:
    """Write the per-tenant LLM key.

    Two locations are kept in sync:

    1. ``soctalk-system/tenant-<id>-llm`` — legacy/audit copy used by
       ``_probe_api_key_present`` and as a backup if the tenant ns is
       gone.
    2. ``tenant-<slug>/tenant-llm-key`` — the *mounted* Secret the
       runs-worker actually reads (chart's
       ``runsWorker.tokenSecretRef``→``llm.apiKeyRef.name``). Without
       this, post-provisioning rotations only update Postgres and the
       worker keeps using the stale credential. Best-effort: a
       missing tenant ns isn't fatal (cross-cluster path materializes
       the Secret via the chart at install time).
    """
    k8s = new_k8s_client()
    await k8s.put_secret(
        _soctalk_system_ns(),
        f"tenant-{tenant_id}-llm",
        data={"api_key": api_key},
        labels={
            "soctalk.io/tenant-id": str(tenant_id),
            "soctalk.io/secret-purpose": "llm",
            "managed-by": "soctalk",
        },
    )
    if tenant_slug:
        try:
            await k8s.put_secret(
                f"tenant-{tenant_slug}",
                "tenant-llm-key",
                data={"api_key": api_key},
                labels={
                    "soctalk.io/tenant-id": str(tenant_id),
                    "soctalk.io/secret-purpose": "llm-api-key",
                    "managed-by": "soctalk",
                },
            )
            # ``secretKeyRef`` env vars don't refresh on Secret update,
            # so the runs-worker would hold the old key in-process. A
            # rolling restart cycles its pods against the new Secret.
            await _restart_runs_worker(tenant_slug)
        except Exception as exc:  # noqa: BLE001
            import structlog
            structlog.get_logger().warning(
                "llm_api_key_tenant_ns_write_failed",
                tenant_id=str(tenant_id),
                tenant_slug=tenant_slug,
                error=str(exc),
            )


async def _restart_runs_worker(tenant_slug: str) -> None:
    """Roll the runs-worker after an LLM Secret change. Best-effort.

    The Deployment name follows the chart's release-named convention
    (``tenant-<slug>-soctalk-runs-worker``); a 404 means the tenant
    runs in cross-cluster mode (this MSSP cluster has no runs-worker
    Deployment to roll) and the kubelet on the L2 cluster will pick
    the new Secret on its own restart cadence.
    """
    try:
        k8s = new_k8s_client()
    except Exception:
        return
    try:
        # Tenant chart renders the Deployment as ``soctalk-runs-worker``
        # (no release-name prefix) in ``tenant-<slug>``.
        await k8s.rollout_restart_deployment(
            f"tenant-{tenant_slug}",
            "soctalk-runs-worker",
        )
    except Exception as exc:  # noqa: BLE001
        import structlog
        structlog.get_logger().warning(
            "llm_runs_worker_restart_failed",
            tenant_slug=tenant_slug,
            error=str(exc),
        )


async def _delete_api_key_secret(tenant_id: UUID, tenant_slug: str | None = None) -> None:
    """Best-effort removal of the LLM Secrets in BOTH locations.

    The legacy/audit copy in ``soctalk-system/tenant-<id>-llm`` and
    the mounted copy in ``tenant-<slug>/tenant-llm-key`` both need
    to be cleared so the runs-worker stops reading a now-revoked
    key. Both deletes are idempotent (404 is treated as success) and
    failures are logged but non-fatal — Postgres is the source of
    truth.
    """
    import structlog
    from kubernetes.client.exceptions import ApiException

    log = structlog.get_logger()

    try:
        k8s = new_k8s_client()
    except Exception:
        return
    # Legacy/audit copy in the soctalk-system namespace.
    try:
        await k8s._run(
            k8s._core.delete_namespaced_secret,
            f"tenant-{tenant_id}-llm",
            _soctalk_system_ns(),
        )
    except ApiException as e:
        if e.status != 404:
            log.warning(
                "llm_api_key_k8s_delete_failed",
                tenant_id=str(tenant_id), status=e.status,
            )
    except Exception as exc:
        log.warning(
            "llm_api_key_k8s_delete_failed",
            tenant_id=str(tenant_id), error=str(exc),
        )

    # Mounted copy in the tenant namespace — what the runs-worker
    # actually reads. Without this delete the worker keeps holding
    # the rotated/cleared key on disk.
    if tenant_slug:
        try:
            await k8s._run(
                k8s._core.delete_namespaced_secret,
                "tenant-llm-key",
                f"tenant-{tenant_slug}",
            )
        except ApiException as e:
            if e.status != 404:
                log.warning(
                    "llm_api_key_tenant_ns_delete_failed",
                    tenant_id=str(tenant_id),
                    tenant_slug=tenant_slug,
                    status=e.status,
                )
        except Exception as exc:
            log.warning(
                "llm_api_key_tenant_ns_delete_failed",
                tenant_id=str(tenant_id),
                tenant_slug=tenant_slug,
                error=str(exc),
            )
        # Restart the runs-worker so it stops holding the cleared key.
        # Best-effort; a 404 (no Deployment in this cluster) is treated
        # as a no-op by the helper.
        try:
            await _restart_runs_worker(tenant_slug)
        except Exception as exc:
            log.warning(
                "llm_runs_worker_restart_failed",
                tenant_slug=tenant_slug,
                error=str(exc),
            )


@router.delete(
    "/{tenant_id}/llm/api-key",
    status_code=204,
    dependencies=[Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))],
)
async def clear_tenant_llm_api_key(
    tenant_id: UUID, request: Request,
) -> None:
    """Explicit clear path for a tenant's LLM API key.

    Primary action is the Postgres column; the K8s Secret is deleted
    best-effort alongside. Idempotent — returns 204 whether or not a
    key was previously set. Use when rotating without immediately
    providing a replacement (operator handoff, tenant churn).
    """
    session = _db(request)
    from soctalk.core.tenancy.models import Tenant as _Tenant

    tenant_slug: str | None = (
        await session.execute(select(_Tenant.slug).where(_Tenant.id == tenant_id))
    ).scalar_one_or_none()
    async with tenant_context(session, tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if cfg is None:
            # Create the row if missing so the clear operation is idempotent
            cfg = IntegrationConfig(tenant_id=tenant_id)
            session.add(cfg)
            await session.flush()
        cfg.llm_api_key_plain = None
        await session.flush()
    # Same DB-first ordering as PATCH: commit the Postgres clear
    # BEFORE the best-effort K8s delete. If the commit fails, the
    # Secret stays on disk — consistent with "Postgres is the source
    # of truth" (reader sees the still-present column + Secret). If
    # we deleted the Secret first and the commit failed after, readers
    # would see the column still set but nothing to mount on
    # collapsed-tier deploys.
    await session.commit()
    await _delete_api_key_secret(tenant_id, tenant_slug)


async def _probe_api_key_present(tenant_id: UUID) -> bool:
    """Diagnostic: does the collapsed-tier LLM Secret exist + have data?

    NOT used by the ``has_api_key`` path — that reads Postgres only,
    so a lingering Secret after a clear can't resurrect the flag. Kept
    as a helper for future operator-facing drift detection (e.g.
    "Secret missing from the cluster but column present" → collapsed-
    tier operator needs to re-seed).

    Best-effort contract: MUST NOT raise. Pure cross-cluster deploys
    run L1 without a reachable MSSP-cluster kubeconfig; the probe
    treats any kube-unavailable condition — client construction
    failure, RBAC denial, API unreachable, 404, empty data — as "not
    present".
    """
    from base64 import b64decode

    from kubernetes.client.exceptions import ApiException

    try:
        k8s = new_k8s_client()
    except Exception:
        # No in-cluster config AND no kubeconfig → L1 runs without a
        # reachable MSSP cluster (pure cross-cluster). Absence.
        return False
    try:
        secret = await k8s._run(
            k8s._core.read_namespaced_secret,
            f"tenant-{tenant_id}-llm",
            _soctalk_system_ns(),
        )
    except ApiException as e:
        # 404 → Secret doesn't exist. 401/403 → RBAC denial, treat as
        # absent so a misconfigured L1 doesn't 500 every read. Same
        # for connection errors (covered by the generic Exception).
        if e.status == 404:
            return False
        import structlog
        structlog.get_logger().debug(
            "llm_api_key_probe_non_404",
            tenant_id=str(tenant_id), status=e.status,
        )
        return False
    except Exception:
        return False
    data = secret.data or {}
    raw = data.get("api_key", "")
    return bool(raw and b64decode(raw).strip())


# ---------------------------------------------------------------------------
# Tenant-side BYOK
# ---------------------------------------------------------------------------


class TenantLlmKeyUpdate(BaseModel):
    """Tenant-pasted API key payload.

    Provider/model are MSSP-controlled — the tenant can't switch from
    Anthropic to OpenAI on a whim because the install's outbound-
    egress allowlist (Cilium policy in render.py) only permits the
    configured provider's hostname. They can only swap the credential.
    """

    api_key: str = Field(min_length=1, max_length=4096)


async def _install_shared_llm_key() -> str | None:
    """Read the MSSP install's shared LLM key.

    Used as the fallback when a tenant clears their per-tenant override
    — the runs-worker's mounted Secret can't simply be deleted (the
    pod would CrashLoop reading a missing file), so we re-mirror the
    install-shared bytes back into ``tenant-<slug>/tenant-llm-key``.

    Returns ``None`` when the install Secret is unreadable (cross-
    cluster path, RBAC denial, missing). Caller should surface this
    as a 409 — the tenant cleared their key but there's no fallback
    to mirror, so the runs-worker would lose its credential.
    """
    src_ns = _soctalk_system_ns()
    src_name = os.getenv(
        "SOCTALK_INSTALL_LLM_SECRET_NAME",
        "soctalk-system-llm-api-key",
    )
    explicit_key = os.getenv("SOCTALK_INSTALL_LLM_SECRET_KEY")
    candidate_keys = (
        [explicit_key]
        if explicit_key
        else ["openai-api-key", "anthropic-api-key", "api_key"]
    )
    try:
        k8s = new_k8s_client()
    except Exception:
        return None
    try:
        from base64 import b64decode

        src = await k8s._run(
            k8s._core.read_namespaced_secret, src_name, src_ns
        )
    except Exception:
        return None
    data = (src.data if hasattr(src, "data") else src.get("data")) or {}
    for key in candidate_keys:
        if not key:
            continue
        raw = data.get(key)
        if raw:
            try:
                return b64decode(raw).decode().strip() or None
            except Exception:
                continue
    return None


async def _tenant_slug(session: AsyncSession, tenant_id: UUID) -> str | None:
    from soctalk.core.tenancy.models import Tenant as _Tenant

    return (
        await session.execute(select(_Tenant.slug).where(_Tenant.id == tenant_id))
    ).scalar_one_or_none()


@tenant_router.get(
    "",
    response_model=LlmConfigRead,
    dependencies=[Depends(require_tenant_role(Role.TENANT_ADMIN))],
)
async def tenant_get_llm(request: Request) -> LlmConfigRead:
    """Tenant-scoped read: provider/model + whether a tenant override
    is set. ``has_api_key=False`` here means "no per-tenant key" — the
    runs-worker is using the MSSP's shared install key (still works,
    just MSSP-funded). ``has_api_key=True`` + the masked preview means
    BYOK is active.
    """
    identity = current_identity(request)
    if identity.tenant_id is None:
        raise HTTPException(400, "tenant_id missing from session")
    session = _db(request)
    async with tenant_context(session, identity.tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == identity.tenant_id
            )
        )).scalar_one_or_none()
    if cfg is None:
        # Tenant hasn't been configured yet — return a default view instead
        default_cfg = IntegrationConfig(tenant_id=identity.tenant_id)
        cfg = default_cfg
    return LlmConfigRead(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        fast_model=cfg.llm_fast_model,
        reasoning_model=cfg.llm_reasoning_model,
        temperature=cfg.llm_temperature,
        max_tokens=cfg.llm_max_tokens,
        dollar_budget_per_run=cfg.llm_dollar_budget_per_run,
        token_budget_per_run=cfg.llm_token_budget_per_run,
        has_api_key=bool(cfg.llm_api_key_plain),
        api_key_preview=_mask_key(cfg.llm_api_key_plain),
        tiers=_sanitize_tiers(cfg.llm_tiers),
    )


@tenant_router.put(
    "/api-key",
    response_model=LlmConfigRead,
    dependencies=[Depends(require_tenant_role(Role.TENANT_ADMIN))],
)
async def tenant_put_llm_key(
    payload: TenantLlmKeyUpdate, request: Request
) -> LlmConfigRead:
    """Tenant pastes their own LLM API key.

    Same dual-write contract as the MSSP path: Postgres FIRST (the
    authoritative store), then best-effort K8s Secret + runs-worker
    rolling restart so the new credential is in use within seconds
    of the call returning.

    Provider and model are NOT settable from this endpoint by design —
    the install's outbound egress policy is provider-pinned, and
    letting a tenant_admin flip provider would either DoS their own
    runs (egress blocked) or require us to re-render the Cilium
    policy at request-time. Both are worse than "MSSP picks the
    provider; tenant supplies their own credential within it".
    """
    identity = current_identity(request)
    tenant_id = identity.tenant_id
    if tenant_id is None:
        raise HTTPException(400, "tenant_id missing from session")
    session = _db(request)
    slug = await _tenant_slug(session, tenant_id)
    async with tenant_context(session, tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if cfg is None:
            # Create the integration_configs row if it doesn't exist so the
            # tenant can set their BYOK key in one step.
            cfg = IntegrationConfig(tenant_id=tenant_id)
            session.add(cfg)
            await session.flush()
        cfg.llm_api_key_plain = payload.api_key
        await session.flush()
    # DB-first ordering — see update_tenant_llm above for the same
    # rationale (rollback safety).
    await session.commit()
    try:
        await _write_api_key(tenant_id, payload.api_key, slug)
    except Exception as exc:  # pragma: no cover — best-effort
        import structlog

        structlog.get_logger().warning(
            "tenant_llm_byok_k8s_write_failed",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
    return LlmConfigRead(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        fast_model=cfg.llm_fast_model,
        reasoning_model=cfg.llm_reasoning_model,
        has_api_key=True,
        api_key_preview=_mask_key(payload.api_key),
    )


@tenant_router.delete(
    "/api-key",
    response_model=LlmConfigRead,
    dependencies=[Depends(require_tenant_role(Role.TENANT_ADMIN))],
)
async def tenant_clear_llm_key(request: Request) -> LlmConfigRead:
    """Revert to the MSSP's shared install key.

    A naive "delete the Secret" path would crash-loop the runs-worker
    (mountPath unreadable). Instead we read the install-shared key
    and re-mirror it into ``tenant-<slug>/tenant-llm-key`` so the
    runs-worker keeps running after restart. If the install key
    isn't readable from this L1 (cross-cluster path, RBAC denial),
    the call fails 409 and the tenant override is preserved — the
    tenant operator chose to clear, but we'd rather refuse than
    leave them with a missing-key crash.
    """
    identity = current_identity(request)
    tenant_id = identity.tenant_id
    if tenant_id is None:
        raise HTTPException(400, "tenant_id missing from session")
    session = _db(request)
    slug = await _tenant_slug(session, tenant_id)
    install_key = await _install_shared_llm_key()
    if install_key is None:
        raise HTTPException(
            409,
            "cannot revert to MSSP key — install-shared LLM key is "
            "not readable from this L1; ask the MSSP operator to "
            "re-seed before clearing the tenant override",
        )
    async with tenant_context(session, tenant_id):
        cfg = (await session.execute(
            select(IntegrationConfig).where(
                IntegrationConfig.tenant_id == tenant_id
            )
        )).scalar_one_or_none()
        if cfg is None:
            # Create a default row so the revert/clear path can operate.
            cfg = IntegrationConfig(tenant_id=tenant_id)
            session.add(cfg)
            await session.flush()
        cfg.llm_api_key_plain = None
        await session.flush()
    await session.commit()
    # Re-mirror the install-shared key into the tenant ns so the
    # runs-worker stays functional. Best-effort: a K8s write failure
    # leaves Postgres clean (the source of truth); the runs-worker
    # will keep using the previously-mounted Secret bytes until the
    # next provision-time copy.
    try:
        await _write_api_key(tenant_id, install_key, slug)
    except Exception as exc:  # pragma: no cover
        import structlog

        structlog.get_logger().warning(
            "tenant_llm_revert_k8s_write_failed",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
    return LlmConfigRead(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        model=cfg.llm_model,
        fast_model=cfg.llm_fast_model,
        reasoning_model=cfg.llm_reasoning_model,
        has_api_key=False,
        api_key_preview="",
    )
