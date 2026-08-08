"""MSSP-side tenant lifecycle endpoints.

Mounted under ``/api/mssp/tenants/*`` by the top-level FastAPI app.
Every handler is guarded by :func:`require_role` on the 3 MSSP roles
(``platform_admin``, ``mssp_admin``, ``analyst``).
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from soctalk.core.llm_provider import (
    infer_provider_from_key,
    normalize_provider,
    reconcile_provider_base_url,
    reconcile_provider_model,
)
from soctalk.core.provisioning import TenantController
from soctalk.core.provisioning.k8s import new_k8s_client
from soctalk.core.tenancy.context import tenant_context
from soctalk.core.tenancy.decorators import require_role
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Organization,
    ProvisioningJob,
    Role,
    Tenant,
    TenantLifecycleEvent,
    TenantState,
)

router = APIRouter(prefix="/api/mssp/tenants", tags=["mssp-tenants"])


# ----- Schemas ---------------------------------------------------------------


class TenantCreate(BaseModel):
    # Reject unknown fields instead of silently dropping them (issue #110):
    # sending the WIZARD payload (profile, external_siem, llm_api_key) here
    # used to 201 a default-poc tenant with none of that material applied,
    # burning the slug. A 422 naming the stray fields steers the caller to
    # POST /api/mssp/tenants/onboard.
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    display_name: str = Field(..., min_length=1, max_length=255)
    # LLM
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_model: str = Field(default="gpt-4o")
    # Integration URLs (optional: chart renders defaults for ns-internal svcs)
    wazuh_url: str | None = None
    thehive_url: str | None = None
    cortex_url: str | None = None
    # Branding
    branding_app_name: str | None = None
    branding_logo_url: str | None = None
    branding_primary_color: str | None = None
    branding_secondary_color: str | None = None


class TenantRead(BaseModel):
    id: UUID
    slug: str
    display_name: str
    state: str
    profile: str | None = None
    created_at: str
    state_changed_at: str
    runtime: dict | None = None


class ExternalSiemOnboard(BaseModel):
    """External SIEM (Wazuh) connection material for the ``provided`` profile.

    The tenant brings their own Wazuh deployment rather than having SocTalk
    provision one in-namespace. The Wazuh **API** (manager, :55000) and the
    **Indexer** (OpenSearch, :9200) authenticate with *separate* credentials,
    mirroring the 4-key ``*-wazuh-creds`` Secret. ``api_token`` is an optional
    pre-minted manager token that overrides username/password auth; its
    absence is always valid. Persisted onto the tenant's IntegrationConfig
    (passwords/token land in the ``*_plain`` columns).
    """

    indexer_url: str = Field(..., max_length=500)
    indexer_username: str = Field(..., max_length=255)
    indexer_password: str = Field(..., max_length=4096)
    api_url: str = Field(..., max_length=500)
    api_username: str = Field(..., max_length=255)
    api_password: str = Field(..., max_length=4096)
    api_token: str | None = Field(default=None, max_length=4096)
    verify_ssl: bool = True


class TenantOnboard(BaseModel):
    """Wizard payload — one POST, three logical steps' worth of data."""

    # Step 1 — identity
    slug: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    display_name: str = Field(..., min_length=1, max_length=255)
    # Step 2 — deployment profile
    profile: str = Field(..., pattern=r"^(poc|persistent|provided)$")
    # Step 3 — branding + contact (all optional, defaults handled server-side)
    branding_app_name: str | None = Field(default=None, max_length=255)
    branding_logo_url: str | None = Field(default=None, max_length=500)
    branding_primary_color: str | None = Field(default=None, max_length=16)
    branding_secondary_color: str | None = Field(default=None, max_length=16)
    contact_email: str | None = Field(default=None, max_length=320)
    # LLM endpoint (optional; tenant can set API key later via detail page)
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_model: str = Field(default="gpt-4o")
    # Optional per-tenant model overrides (runs-worker SOCTALK_FAST_MODEL /
    # SOCTALK_REASONING_MODEL). Blank/whitespace-only values normalize to
    # None so the IntegrationConfig columns stay NULL and render falls back
    # to ``llm_model``. Never defaulted to a concrete model server-side.
    llm_fast_model: str | None = Field(default=None, max_length=255)
    llm_reasoning_model: str | None = Field(default=None, max_length=255)
    # Per-tenant LLM credentials. REQUIRED for ``provided`` (enforced
    # server-side with a field-level 422 in :func:`_llm_key_errors` before
    # any DB read/write); optional for poc/persistent, where the
    # install-shared key fallback in the controller still applies.
    # ``llm_provider`` is normalized openai → openai-compatible (same
    # canonicalization as ``LlmConfigUpdate``); when omitted alongside a key
    # it is inferred from the key's vendor prefix at onboard time.
    llm_api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    llm_provider: str | None = Field(
        default=None,
        pattern=r"^(openai|anthropic|openai-compatible)$",
    )
    # External SIEM connection — only meaningful for the ``provided`` profile.
    # Required for ``provided`` (enforced server-side with a 422 in
    # :func:`_validate_external_siem`); ignored entirely for poc/persistent so
    # the controller fills wazuh_url/indexer_url in-cluster.
    external_siem: ExternalSiemOnboard | None = None

    @field_validator("llm_provider")
    @classmethod
    def _normalize_llm_provider(cls, v: str | None) -> str | None:
        # Shared canonicalization with LlmConfigUpdate (llm_config.py) —
        # storage must only ever see ``openai-compatible`` / ``anthropic``
        # so chart values.schema.json validation never fails on render.
        return normalize_provider(v)

    @field_validator("llm_fast_model", "llm_reasoning_model")
    @classmethod
    def _blank_model_override_is_none(cls, v: str | None) -> str | None:
        # Blank/whitespace-only overrides mean "no override": keep the
        # column NULL so render's ``or llm_model`` fallback applies and the
        # provider reconciliation below never runs on an empty string.
        if v is not None and not v.strip():
            return None
        return v


class ProvisioningJobRead(BaseModel):
    id: UUID
    tenant_id: UUID
    kind: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    next_attempt_at: str


class LifecycleEventRead(BaseModel):
    id: UUID
    timestamp: str
    event_type: str
    from_state: str | None
    to_state: str | None
    actor_id: str | None
    details: dict


# ----- Helpers ---------------------------------------------------------------


async def _get_organization(session: AsyncSession) -> Organization:
    # An install has exactly one Organization, but order deterministically so
    # that if more ever exist (e.g. a botched re-bootstrap) new tenants attach
    # to a stable org — the oldest — rather than a nondeterministic row.
    result = await session.execute(
        select(Organization).order_by(Organization.created_at.asc()).limit(1)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(500, "install not bootstrapped (no Organization row)")
    return org


def _db(request: Request) -> AsyncSession:
    session = getattr(request.state, "db", None)
    if session is None:
        raise HTTPException(500, "db session not attached to request")
    return session


# Fields of ``external_siem`` that are mandatory when profile='provided'.
# ``api_token`` is deliberately excluded — it is an optional override and its
# absence (or emptiness) must never trigger a 422. ``verify_ssl`` is a bool
# with a default and is likewise never "missing".
_REQUIRED_EXTERNAL_SIEM_FIELDS: tuple[str, ...] = (
    "indexer_url",
    "indexer_username",
    "indexer_password",
    "api_url",
    "api_username",
    "api_password",
)


def _external_siem_errors(siem: ExternalSiemOnboard | None) -> list[dict]:
    """Return field-level validation errors for a ``provided`` onboard.

    When ``siem`` is ``None`` every required field is reported missing. When a
    block is present, only the empty/blank required fields are reported. An
    empty list means the external SIEM material is complete.
    """
    fields = (
        _REQUIRED_EXTERNAL_SIEM_FIELDS
        if siem is None
        else tuple(
            name
            for name in _REQUIRED_EXTERNAL_SIEM_FIELDS
            if not (getattr(siem, name) or "").strip()
        )
    )
    return [
        {
            "loc": ["body", "external_siem", name],
            "msg": f"{name} is required when profile='provided'",
            "type": "value_error.missing",
        }
        for name in fields
    ]


def _validate_external_siem(siem: ExternalSiemOnboard | None) -> None:
    """Reject a ``provided`` onboard that lacks complete external-SIEM creds.

    Raises :class:`HTTPException` (422) with field-level errors when the
    ``external_siem`` block is absent or any required field is empty/blank.
    Must be called before any DB write so a rejected onboard creates NO
    Tenant row.
    """
    errors = _external_siem_errors(siem)
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _llm_key_errors(llm_api_key: str | None) -> list[dict[str, object]]:
    """Field-level error for a ``provided`` onboard missing its LLM key.

    The ``provided`` profile has no install-shared fallback contract — the
    tenant brings their own SIEM *and* their own LLM credential — so a
    missing/blank ``llm_api_key`` must be rejected with the same field-level
    422 shape as :func:`_external_siem_errors`, BEFORE any DB read/write.
    The key value itself is never reflected into the error detail.
    """
    if (llm_api_key or "").strip():
        return []
    return [
        {
            "loc": ["body", "llm_api_key"],
            "msg": "llm_api_key is required when profile='provided'",
            "type": "value_error.missing",
        }
    ]


# ----- Endpoints --------------------------------------------------------------


def _install_default_llm_tiers() -> dict[str, Any] | None:
    """Install-wide default per-tier LLM backends for newly onboarded tenants.

    Reads ``SOCTALK_DEFAULT_FAST_TIER_*`` env (set from the chart's
    ``defaults.llm.fastTier``). When a provider/base_url/model are all present,
    returns a validated ``llm_tiers`` map routing the FAST (router) tier to that
    backend — e.g. a self-hosted Qwen on Modal/sglang — leaving the reasoning
    verdict on the tenant's primary provider. Returns None when unconfigured, so
    installs without the env behave exactly as before (single-provider).
    """
    provider = os.getenv("SOCTALK_DEFAULT_FAST_TIER_PROVIDER", "").strip()
    base_url = os.getenv("SOCTALK_DEFAULT_FAST_TIER_BASE_URL", "").strip()
    model = os.getenv("SOCTALK_DEFAULT_FAST_TIER_MODEL", "").strip()
    if not (provider and base_url and model):
        return None
    block: dict[str, Any] = {"provider": provider, "base_url": base_url, "model": model}
    for env_key, field in (
        ("SOCTALK_DEFAULT_FAST_TIER_ENGINE", "engine"),
        ("SOCTALK_DEFAULT_FAST_TIER_DECODING_MODE", "decoding_mode"),
        ("SOCTALK_DEFAULT_FAST_TIER_API_KEY", "api_key_plain"),
    ):
        val = os.getenv(env_key, "").strip()
        if val:
            block[field] = val
    from soctalk.core.tenancy.models import validate_llm_tiers
    return validate_llm_tiers({"fast": block})


@router.post(
    "/onboard",
    response_model=TenantRead,
    status_code=202,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def onboard_tenant(
    payload: TenantOnboard,
    request: Request,
) -> TenantRead:
    """Async tenant onboarding: writes initial rows, enqueues a provisioning
    job, and returns immediately with state=``provisioning``.

    The worker picks up the job, runs the stepwise reconcile on
    :class:`TenantController`, and flips the tenant to ``active`` when
    the namespace + secrets + Helm releases + workloads are all ready.
    """
    session = _db(request)

    # Validate external SIEM material + the per-tenant LLM key up front for
    # the ``provided`` profile, *before* any DB read/write, so a rejected
    # onboard creates NO Tenant row. Errors are combined so the wizard
    # surfaces every missing field in one round-trip.
    if payload.profile == "provided":
        errors = _external_siem_errors(payload.external_siem)
        errors += _llm_key_errors(payload.llm_api_key)
        if errors:
            raise HTTPException(status_code=422, detail=errors)

    org = await _get_organization(session)

    existing = await session.execute(
        select(Tenant).where(Tenant.slug == payload.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"tenant slug '{payload.slug}' already exists")

    tenant = Tenant(
        slug=payload.slug,
        display_name=payload.display_name,
        state=TenantState.PENDING.value,
        profile=payload.profile,
        organization_id=org.id,
        config={"contact_email": payload.contact_email} if payload.contact_email else {},
    )
    session.add(tenant)
    await session.flush()

    # Per-tenant LLM credentials. When a key is supplied without an explicit
    # provider, infer it from the key's vendor prefix (sk-ant- → anthropic,
    # else the openai-compatible default) and flip a clearly-mismatched
    # default model via the same shared helper the controller uses — so an
    # sk-ant- onboard never renders SOCTALK_FAST_MODEL=gpt-4o on the
    # runs-worker. The raw key is NEVER logged or echoed in any response.
    llm_provider = payload.llm_provider  # already normalized by the validator
    llm_model: str = payload.llm_model
    llm_base_url: str = payload.llm_base_url
    # Optional per-tenant overrides (blank already normalized to None by the
    # schema validator). Omitted overrides stay None — never defaulted to a
    # concrete model — so the columns stay NULL and render falls back to
    # llm_model.
    llm_fast_model = payload.llm_fast_model
    llm_reasoning_model = payload.llm_reasoning_model
    if payload.llm_api_key and llm_provider is None:
        llm_provider = infer_provider_from_key(payload.llm_api_key)
    # Install-shared LLM defaults (set by the soctalk-system chart from
    # ``defaults.llm.*`` values, sourced in turn from SOCTALK_LLM_PROVIDER
    # at install time). When neither the wizard's "LLM (advanced)"
    # disclosure nor a key-with-inferred-provider supplied one, fall back
    # here — so an MSSP that installed with anthropic doesn't see new
    # tenants come up with openai-compatible/gpt-4o. When provider falls
    # through to the env default and the operator didn't pick a model
    # either (the field is still the schema's gpt-4o sentinel),
    # reconcile model with the env-provided default so the pair stays
    # internally consistent.
    if llm_provider is None:
        import os
        env_provider = os.getenv("SOCTALK_LLM_PROVIDER_DEFAULT", "").strip()
        if env_provider:
            llm_provider = env_provider
            if llm_model == "gpt-4o":
                env_model = os.getenv("SOCTALK_LLM_MODEL_DEFAULT", "").strip()
                if env_model:
                    llm_model = env_model
            # Same sentinel-swap for the endpoint: the wizard prefills the
            # OpenAI base_url even when the operator never opened "LLM
            # (advanced)", so a provider that fell through to the install
            # default (e.g. anthropic) would otherwise persist
            # ``https://api.openai.com/v1`` on the row. Harmless at runtime
            # (the anthropic client uses ANTHROPIC_BASE_URL, not this field)
            # but confusing in the tenant detail UI — replace it with the
            # install's own base_url default so the stored row is coherent.
            if llm_base_url == "https://api.openai.com/v1":
                env_base_url = os.getenv("SOCTALK_LLM_BASE_URL_DEFAULT", "").strip()
                if env_base_url:
                    llm_base_url = env_base_url
    if llm_provider is not None:
        llm_model = reconcile_provider_model(llm_provider, llm_model) or llm_model
        # Keep the endpoint consistent with the resolved provider. The env
        # fallthrough above already prefers SOCTALK_LLM_BASE_URL_DEFAULT for
        # the no-key inherit path; this ALSO covers the key-inferred path
        # (an ``sk-ant-`` key → provider=anthropic while base_url is still the
        # OpenAI sentinel), where a mismatched base_url would open the
        # runs-worker egress NetworkPolicy for api.openai.com and drop the
        # Anthropic call. No-op once base_url no longer equals the sentinel.
        llm_base_url = reconcile_provider_base_url(llm_provider, llm_base_url)
        # Same only-flip-when-clearly-mismatched rule for the overrides: an
        # sk-ant- onboard carrying llm_fast_model=gpt-4o-mini must not render
        # an OpenAI model on the runs-worker, while a matching 'claude-*'
        # override is preserved verbatim.
        if llm_fast_model is not None:
            llm_fast_model = reconcile_provider_model(llm_provider, llm_fast_model)
        if llm_reasoning_model is not None:
            llm_reasoning_model = reconcile_provider_model(
                llm_provider, llm_reasoning_model
            )
    # Only pass llm_provider when set so the column default
    # ('openai-compatible') applies for a provider-less, key-less onboard.
    llm_kwargs: dict[str, Any] = {"llm_model": llm_model}
    if llm_provider is not None:
        llm_kwargs["llm_provider"] = llm_provider
    if payload.llm_api_key is not None:
        llm_kwargs["llm_api_key_plain"] = payload.llm_api_key
    if llm_fast_model is not None:
        llm_kwargs["llm_fast_model"] = llm_fast_model
    if llm_reasoning_model is not None:
        llm_kwargs["llm_reasoning_model"] = llm_reasoning_model
    # Install-wide default per-tier backend (issue #4/#12): when the install
    # configures a default FAST tier (SOCTALK_DEFAULT_FAST_TIER_* env) and the
    # onboard didn't specify its own tiers, every new tenant's router loop runs
    # on that shared backend (e.g. a self-hosted Qwen on Modal/sglang) while the
    # reasoning verdict stays on the primary frontier provider. Injected into the
    # DB llm_tiers so the provisioning controller materializes the tier's own key
    # into tenant-llm-key/fast_api_key.
    default_tiers = _install_default_llm_tiers()
    if default_tiers is not None:
        llm_kwargs["llm_tiers"] = default_tiers

    async with tenant_context(session, tenant.id):
        # External SIEM connection material is only captured for the
        # 'provided' profile (BYO-SIEM, validated above). For poc/persistent
        # ``siem`` is None and these columns stay NULL so the controller fills
        # wazuh_url/indexer_url in-cluster. Lands in the SAME transaction as
        # the Tenant row (single commit below) — as do the per-tenant LLM
        # credentials above.
        siem = payload.external_siem if payload.profile == "provided" else None
        session.add_all([
            IntegrationConfig(
                tenant_id=tenant.id,
                llm_base_url=llm_base_url,
                **llm_kwargs,
                wazuh_indexer_url=siem.indexer_url if siem else None,
                wazuh_indexer_username=siem.indexer_username if siem else None,
                wazuh_indexer_password_plain=(
                    siem.indexer_password if siem else None
                ),
                wazuh_api_url=siem.api_url if siem else None,
                wazuh_username=siem.api_username if siem else None,
                wazuh_password_plain=siem.api_password if siem else None,
                wazuh_api_token_plain=siem.api_token if siem else None,
                wazuh_verify_ssl=siem.verify_ssl if siem else True,
            ),
            BrandingConfig(
                tenant_id=tenant.id,
                app_name=payload.branding_app_name or payload.display_name,
                logo_url=payload.branding_logo_url,
                primary_color=payload.branding_primary_color,
                secondary_color=payload.branding_secondary_color,
            ),
        ])
        # Enqueue the provisioning job. The partial unique index on
        # provisioning_jobs makes re-enqueue idempotent if this POST
        # is retried client-side.
        session.add(
            ProvisioningJob(
                tenant_id=tenant.id,
                kind="tenant.provision",
                status="pending",
            )
        )
        # Lifecycle event so the wizard landing immediately sees something.
        session.add(
            TenantLifecycleEvent(
                tenant_id=tenant.id,
                event_type="onboard_submitted",
                from_state=None,
                to_state=TenantState.PENDING.value,
                actor_id=str(request.state.user_identity.get("user_id")),
                details={"profile": payload.profile},
            )
        )
        await session.commit()

    return _to_read(tenant)


@router.post(
    "/{tenant_id}:retry",
    response_model=ProvisioningJobRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def retry_provisioning(tenant_id: UUID, request: Request) -> ProvisioningJobRead:
    """Retry provisioning for a degraded tenant.

    Reopens a failed ``tenant.provision`` job (or inserts a fresh one),
    but first handles the still-ACTIVE-job window: the worker flips the
    tenant to degraded on the FIRST ProvisionError while the job only
    reaches status='failed' after max_attempts. During that backoff
    window a pending/in_flight row still exists, and blind-inserting a
    duplicate would violate the partial unique index
    ``uq_provisioning_jobs_active`` (500). Same pre-check pattern as
    ``llm_config.update_tenant_llm``.
    """
    session = _db(request)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    if tenant.state != TenantState.DEGRADED.value:
        raise HTTPException(
            409, f"retry is only valid from state=degraded (current: {tenant.state})"
        )

    # Pre-check for an ACTIVE job BEFORE the failed-job lookup — this is
    # the path that used to blow up on uq_provisioning_jobs_active.
    # (type: ignore — SQLModel column-expression false positives, same
    # class as every other .where() in this module.)
    active_job = (
        await session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.tenant_id == tenant_id)  # type: ignore[arg-type]
            .where(ProvisioningJob.kind == "tenant.provision")  # type: ignore[arg-type]
            .where(ProvisioningJob.status.in_(["pending", "in_flight"]))  # type: ignore[attr-defined]
            .order_by(ProvisioningJob.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
    ).scalar_one_or_none()

    # Only when no active job exists: look for a previous failed job we
    # can reopen, else insert a fresh one.
    failed_job = None
    if active_job is None:
        failed_job = (
            await session.execute(
                select(ProvisioningJob)
                .where(ProvisioningJob.tenant_id == tenant_id)
                .where(ProvisioningJob.kind == "tenant.provision")
                .where(ProvisioningJob.status == "failed")
                .order_by(ProvisioningJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async with tenant_context(session, tenant_id):
        if active_job is not None:
            if active_job.status == "pending":
                # Mid-backoff: "retry now" just short-circuits the
                # remaining wait so the worker picks the job up on its
                # next poll. Attempts/last_error are preserved — this is
                # the same run, only sooner.
                active_job.next_attempt_at = datetime.utcnow()
                active_job.updated_at = datetime.utcnow()
                job_action = "backoff_short_circuited"
            else:
                # in_flight: a worker is executing it right now. Leave
                # the claim fields untouched and report it back as-is.
                job_action = "already_in_flight"
            job = active_job
        elif failed_job is not None:
            failed_job.status = "pending"
            failed_job.attempts = 0
            failed_job.last_error = None
            failed_job.claimed_at = None
            failed_job.claimed_by = None
            failed_job.next_attempt_at = datetime.utcnow()
            failed_job.updated_at = datetime.utcnow()
            job = failed_job
            job_action = "reopened_failed"
        else:
            job = ProvisioningJob(
                tenant_id=tenant_id,
                kind="tenant.provision",
                status="pending",
            )
            session.add(job)
            job_action = "enqueued_new"
        session.add(
            TenantLifecycleEvent(
                tenant_id=tenant_id,
                event_type="retry_requested",
                from_state=tenant.state,
                to_state=tenant.state,
                actor_id=str(request.state.user_identity.get("user_id")),
                details={"job_action": job_action},
            )
        )
        await session.commit()

    return ProvisioningJobRead(
        id=job.id,
        tenant_id=job.tenant_id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        last_error=job.last_error,
        next_attempt_at=job.next_attempt_at.isoformat(),
    )


@router.post(
    "",
    response_model=TenantRead,
    status_code=201,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def create_tenant(
    payload: TenantCreate,
    request: Request,
) -> TenantRead:
    session = _db(request)
    org = await _get_organization(session)

    # Enforce slug uniqueness (RLS not relevant: tenants table is install-scoped).
    existing = await session.execute(
        select(Tenant).where(Tenant.slug == payload.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"tenant slug '{payload.slug}' already exists")

    tenant = Tenant(
        slug=payload.slug,
        display_name=payload.display_name,
        state=TenantState.PENDING.value,
        organization_id=org.id,
        config={},
    )
    session.add(tenant)
    await session.flush()

    async with tenant_context(session, tenant.id):
        session.add_all([
            IntegrationConfig(
                tenant_id=tenant.id,
                llm_base_url=payload.llm_base_url,
                llm_model=payload.llm_model,
                wazuh_url=payload.wazuh_url,
                thehive_url=payload.thehive_url,
                cortex_url=payload.cortex_url,
            ),
            BrandingConfig(
                tenant_id=tenant.id,
                app_name=payload.branding_app_name or payload.display_name,
                logo_url=payload.branding_logo_url,
                primary_color=payload.branding_primary_color,
                secondary_color=payload.branding_secondary_color,
            ),
        ])
        await session.flush()

    # Identity-only: no inline helm, no worker enqueue. Programmatic
    # callers that want the data plane to come up should use the
    # ``/onboard`` wizard path (which enqueues a ProvisioningJob) or
    # issue a ``:retry`` once they're ready. Keeping this endpoint
    # inline would fork lifecycle behavior with /onboard.
    await session.commit()
    return _to_read(tenant)


@router.get(
    "",
    response_model=list[TenantRead],
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN, Role.MSSP_MANAGER, Role.ANALYST))
    ],
)
async def list_tenants(request: Request) -> list[TenantRead]:
    session = _db(request)
    result = await session.execute(select(Tenant).where(Tenant.deleted_at.is_(None)))
    tenants = result.scalars().all()
    return [
        TenantRead(
            id=t.id,
            slug=t.slug,
            display_name=t.display_name,
            state=t.state,
            profile=t.profile,
            created_at=t.created_at.isoformat(),
            state_changed_at=t.state_changed_at.isoformat(),
            runtime=t.runtime,
        )
        for t in tenants
    ]


@router.get(
    "/{tenant_id}",
    response_model=TenantRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN, Role.MSSP_MANAGER, Role.ANALYST))
    ],
)
async def get_tenant(tenant_id: UUID, request: Request) -> TenantRead:
    session = _db(request)
    tenant = (await session.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    return TenantRead(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
        state=tenant.state,
        profile=tenant.profile,
        created_at=tenant.created_at.isoformat(),
        state_changed_at=tenant.state_changed_at.isoformat(),
        runtime=tenant.runtime,
    )


@router.post(
    "/{tenant_id}:suspend",
    response_model=TenantRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def suspend_tenant(tenant_id: UUID, request: Request) -> TenantRead:
    session = _db(request)
    controller = TenantController(session)
    async with tenant_context(session, tenant_id):
        tenant = await controller.suspend(
            tenant_id,
            actor_id=str(request.state.user_identity.get("user_id")),
        )
    return _to_read(tenant)


@router.post(
    "/{tenant_id}:resume",
    response_model=TenantRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def resume_tenant(tenant_id: UUID, request: Request) -> TenantRead:
    session = _db(request)
    controller = TenantController(session)
    async with tenant_context(session, tenant_id):
        tenant = await controller.resume(
            tenant_id,
            actor_id=str(request.state.user_identity.get("user_id")),
        )
    return _to_read(tenant)


@router.post(
    "/{tenant_id}:decommission",
    response_model=TenantRead,
    status_code=202,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def decommission_tenant(
    tenant_id: UUID, request: Request, force: bool = False
) -> TenantRead:
    """Enqueue tear-down of a tenant's data plane.

    Flips the tenant to ``decommissioning`` + records a
    ``decommission_started`` lifecycle event synchronously, then
    enqueues a ``tenant.decommission`` job for the provisioning worker
    to drive. Returns 202 immediately so the wizard/UI can poll.
    """
    session = _db(request)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    # Idempotency: don't double-enqueue if one is already active.
    already_queued = (
        await session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.tenant_id == tenant_id)
            .where(ProvisioningJob.kind == "tenant.decommission")
            .where(ProvisioningJob.status.in_(("pending", "in_flight")))
            .limit(1)
        )
    ).scalar_one_or_none()

    async with tenant_context(session, tenant_id):
        if tenant.state not in (
            TenantState.DECOMMISSIONING.value,
            TenantState.ARCHIVED.value,
            TenantState.PURGED.value,
        ):
            from_state = tenant.state
            tenant.state = TenantState.DECOMMISSIONING.value
            session.add(
                TenantLifecycleEvent(
                    tenant_id=tenant.id,
                    event_type="decommission_started",
                    from_state=from_state,
                    to_state=TenantState.DECOMMISSIONING.value,
                    actor_id=str(request.state.user_identity.get("user_id")),
                    details={"force": force},
                )
            )

        if already_queued is None:
            session.add(
                ProvisioningJob(
                    tenant_id=tenant_id,
                    kind="tenant.decommission",
                    status="pending",
                )
            )

        await session.commit()

    return _to_read(tenant)


@router.get(
    "/{tenant_id}/events",
    response_model=list[LifecycleEventRead],
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN, Role.MSSP_MANAGER, Role.ANALYST))
    ],
)
async def list_events(
    tenant_id: UUID, request: Request, limit: int = 100
) -> list[LifecycleEventRead]:
    session = _db(request)
    async with tenant_context(session, tenant_id):
        result = await session.execute(
            select(TenantLifecycleEvent)
            .where(TenantLifecycleEvent.tenant_id == tenant_id)
            .order_by(TenantLifecycleEvent.timestamp.desc())
            .limit(limit)
        )
    evts = result.scalars().all()
    return [
        LifecycleEventRead(
            id=e.id,
            timestamp=e.timestamp.isoformat(),
            event_type=e.event_type,
            from_state=e.from_state,
            to_state=e.to_state,
            actor_id=e.actor_id,
            details=e.details,
        )
        for e in evts
    ]


def _to_read(t: Tenant) -> TenantRead:
    return TenantRead(
        id=t.id,
        slug=t.slug,
        display_name=t.display_name,
        state=t.state,
        profile=t.profile,
        created_at=t.created_at.isoformat(),
        state_changed_at=t.state_changed_at.isoformat(),
        runtime=t.runtime,
    )


# ---------------------------------------------------------------------------
# External SIEM (Wazuh) connection — profile-agnostic PATCH/GET.
# ---------------------------------------------------------------------------
#
# Mirrors the ``llm_config.update_tenant_llm`` dual-write contract: Postgres
# is authoritative and committed FIRST; the K8s Secret rewrite + adapter
# rolling restart are best-effort side effects that log-and-continue on
# failure. The endpoint deliberately does NOT gate on ``tenant.profile`` — an
# existing poc / persistent tenant can be repointed at an external Wazuh after
# the fact (effectively a manual migration).


class ExternalSiemPatch(BaseModel):
    """All-optional external-SIEM credential patch.

    Only non-None fields are written, so a caller can rotate a single
    password (or flip ``verify_ssl``) without resending the full connection
    block. ``None`` — not falsiness — is the "leave unchanged" sentinel, so
    ``verify_ssl=False`` is a real write.
    """

    indexer_url: str | None = Field(default=None, max_length=500)
    indexer_username: str | None = Field(default=None, max_length=255)
    indexer_password: str | None = Field(default=None, max_length=4096)
    api_url: str | None = Field(default=None, max_length=500)
    api_username: str | None = Field(default=None, max_length=255)
    api_password: str | None = Field(default=None, max_length=4096)
    api_token: str | None = Field(default=None, max_length=4096)
    verify_ssl: bool | None = None


class ExternalSiemRead(BaseModel):
    """Masked view of the external-SIEM config.

    Plaintext passwords/token are NEVER returned — only ``has_*`` booleans
    signal their presence (mirrors the ``has_api_key`` precedent in
    ``llm_config.LlmConfigRead``).
    """

    indexer_url: str | None
    indexer_username: str | None
    api_url: str | None
    api_username: str | None
    has_indexer_password: bool
    has_api_password: bool
    has_api_token: bool
    verify_ssl: bool


# payload field → IntegrationConfig column. ``api_*`` map to the Wazuh
# **manager** (API) columns; ``indexer_*`` to the **indexer** columns; both
# HTTP-Basic pairs are distinct, matching the 4-key external-SIEM Secret.
_EXTERNAL_SIEM_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("indexer_url", "wazuh_indexer_url"),
    ("indexer_username", "wazuh_indexer_username"),
    ("indexer_password", "wazuh_indexer_password_plain"),
    ("api_url", "wazuh_api_url"),
    ("api_username", "wazuh_username"),
    ("api_password", "wazuh_password_plain"),
    ("api_token", "wazuh_api_token_plain"),
    ("verify_ssl", "wazuh_verify_ssl"),
)


def _external_siem_read(cfg: IntegrationConfig) -> ExternalSiemRead:
    return ExternalSiemRead(
        indexer_url=cfg.wazuh_indexer_url,
        indexer_username=cfg.wazuh_indexer_username,
        api_url=cfg.wazuh_api_url,
        api_username=cfg.wazuh_username,
        has_indexer_password=bool(cfg.wazuh_indexer_password_plain),
        has_api_password=bool(cfg.wazuh_password_plain),
        has_api_token=bool(cfg.wazuh_api_token_plain),
        verify_ssl=cfg.wazuh_verify_ssl,
    )


def _external_siem_secret_data(cfg: IntegrationConfig) -> dict[str, str]:
    """Build the UPPERCASE-keyed Secret payload from the merged row.

    Same key shape the controller's ``_step_write_external_siem_secret``
    writes so the adapter and the chat resolver read one consistent contract.
    A NULL/empty column drops its key — notably ``WAZUH_API_TOKEN`` when no
    pre-minted token is set, so a NULL column never materializes an empty
    env var.
    """
    data: dict[str, str] = {}
    if cfg.wazuh_indexer_username:
        data["INDEXER_USERNAME"] = cfg.wazuh_indexer_username
    if cfg.wazuh_indexer_password_plain:
        data["INDEXER_PASSWORD"] = cfg.wazuh_indexer_password_plain
    if cfg.wazuh_username:
        data["WAZUH_API_USERNAME"] = cfg.wazuh_username
    if cfg.wazuh_password_plain:
        data["WAZUH_API_PASSWORD"] = cfg.wazuh_password_plain
    if cfg.wazuh_api_token_plain:
        data["WAZUH_API_TOKEN"] = cfg.wazuh_api_token_plain
    return data


@router.get(
    "/{tenant_id}/external-siem",
    response_model=ExternalSiemRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def get_tenant_external_siem(
    tenant_id: UUID, request: Request
) -> ExternalSiemRead:
    """Masked read of a tenant's external-SIEM connection material."""
    session = _db(request)
    async with tenant_context(session, tenant_id):
        cfg = (
            await session.execute(
                select(IntegrationConfig).where(
                    IntegrationConfig.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
    if cfg is None:
        raise HTTPException(404, "tenant has no integration config")
    return _external_siem_read(cfg)


@router.patch(
    "/{tenant_id}/external-siem",
    response_model=ExternalSiemRead,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def update_tenant_external_siem(
    tenant_id: UUID, payload: ExternalSiemPatch, request: Request
) -> ExternalSiemRead:
    """Profile-agnostic external-SIEM update.

    Dual-write ordering (mirrors ``llm_config.update_tenant_llm:267-270``):

      1. Apply non-None payload fields to the ``IntegrationConfig.wazuh_*``
         columns + flush, inside ``tenant_context``.
      2. ``session.commit()`` — Postgres is authoritative and lands BEFORE
         any K8s side effect.
      3. Re-read the merged row, write/patch
         ``Secret/tenant-external-siem-creds`` in ``tenant-<slug>`` (token key
         omitted when unset).
      4. Patch the ``soctalk-adapter`` Deployment annotation so its pod rolls
         and mounts the freshly-written Secret.

    Steps 3-4 are best-effort: a K8s failure is logged via structlog and does
    NOT roll back the committed Postgres update — the user's intent is already
    recorded authoritatively, and the provisioning worker re-reconciles the
    Secret on its next pass.
    """
    session = _db(request)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    async with tenant_context(session, tenant_id):
        cfg = (
            await session.execute(
                select(IntegrationConfig).where(
                    IntegrationConfig.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if cfg is None:
            raise HTTPException(404, "tenant has no integration config")
        for src, dest in _EXTERNAL_SIEM_FIELD_MAP:
            value = getattr(payload, src)
            if value is not None:
                setattr(cfg, dest, value)
        await session.flush()
    # Commit the Postgres row FIRST — before any K8s side effect. The
    # DB-session middleware would otherwise commit after the handler returns,
    # which could leave the K8s Secret written while a late commit failure
    # rolled back the columns (violating "Postgres is authoritative").
    await session.commit()

    # Re-read the merged row so the Secret + response reflect the FULL state,
    # not just the PATCH delta.
    async with tenant_context(session, tenant_id):
        cfg = (
            await session.execute(
                select(IntegrationConfig).where(
                    IntegrationConfig.tenant_id == tenant_id
                )
            )
        ).scalar_one()

    await _apply_external_siem_k8s(tenant_id, tenant.slug, cfg)
    return _external_siem_read(cfg)


async def _apply_external_siem_k8s(
    tenant_id: UUID, tenant_slug: str, cfg: IntegrationConfig
) -> None:
    """Best-effort K8s side effects: rewrite the creds Secret + roll the
    adapter Deployment.

    Every failure is logged via structlog and swallowed — Postgres already
    holds the authoritative update. Construction of the client itself is
    guarded too: a pure cross-cluster L1 has no reachable MSSP-cluster
    kubeconfig and must not 500 the PATCH.
    """
    log = structlog.get_logger()
    namespace = f"tenant-{tenant_slug}"
    try:
        k8s = new_k8s_client()
    except Exception as exc:  # noqa: BLE001 — no reachable cluster
        log.warning(
            "external_siem_k8s_unavailable",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return

    data = _external_siem_secret_data(cfg)
    if data:
        try:
            await k8s.put_secret(
                namespace,
                "tenant-external-siem-creds",
                data=data,
                labels={
                    "soctalk.io/tenant-id": str(tenant_id),
                    "soctalk.io/secret-purpose": "external-siem-creds",
                    "managed-by": "soctalk",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "external_siem_secret_write_failed",
                tenant_id=str(tenant_id),
                error=str(exc),
            )

    # ``secretKeyRef`` env vars don't refresh on a Secret update, so roll the
    # long-lived pods that consume this Secret: the adapter (alert ingest) AND
    # the runs-worker (Wazuh MCP enrichment, issue #109). Patch a pod-template
    # annotation (what ``kubectl rollout restart`` does under the hood) — no
    # rollout subresource perm required. The chat resolver reads creds live
    # per request and needs no restart.
    for _deploy in ("soctalk-adapter", "soctalk-runs-worker"):
        try:
            await k8s.patch_deployment(
                namespace=namespace,
                name=_deploy,
                patch={
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "soctalk.io/restartedAt": str(time.time_ns())
                                }
                            }
                        }
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "external_siem_restart_failed",
                tenant_id=str(tenant_id),
                deployment=_deploy,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Adapter ingest status — server-side proxy to the tenant adapter.
# ---------------------------------------------------------------------------
#
# The detail-page External-SIEM panel polls live ingest status. The BROWSER
# cannot reach the per-tenant adapter Service (cluster-internal DNS, no
# ingress, no CORS), so the MSSP control plane — which IS on the cluster
# network — proxies to the adapter's ``/health/ready`` and relays the JSON.
# A wedged / absent adapter degrades SOFTLY (HTTP 200 + ``reachable: false``)
# so a 10s poll never surfaces a red API error on a healthy detail page.

# Cluster-internal readiness endpoint of a tenant's adapter Deployment. The
# Service is ``soctalk-adapter`` in namespace ``tenant-<slug>`` on port 8080
# (matching the chart's adapter Service). Reachable only from inside the
# cluster — hence the server-side proxy rather than a browser fetch.
_ADAPTER_STATUS_PORT = 8080
_ADAPTER_STATUS_PATH = "/health/ready"


def _adapter_status_url(tenant_slug: str) -> str:
    return (
        f"http://soctalk-adapter.tenant-{tenant_slug}"
        f".svc.cluster.local:{_ADAPTER_STATUS_PORT}{_ADAPTER_STATUS_PATH}"
    )


def _new_adapter_http_client() -> httpx.AsyncClient:
    """Build the httpx client used to reach a tenant adapter.

    Short timeouts so an unreachable / wedged adapter can't stall the detail
    page poll. Factored out as a seam the tests monkeypatch to mock the
    outbound call (no real adapter required).
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))


@router.get(
    "/{tenant_id}/adapter-status",
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def get_tenant_adapter_status(tenant_id: UUID, request: Request) -> dict:
    """Server-side proxy to the tenant adapter's ``/health/ready``.

    Relays the adapter JSON verbatim on success (``ok``, ``alerts_forwarded``,
    ``last_alert_ts``, ``last_ingest_error`` …), defaulting ``reachable`` to
    ``True`` so the poller has one uniform field to branch on. On ANY failure
    (DNS, connect timeout, non-2xx, malformed body) returns HTTP 200 with a
    soft-failure body ``{"reachable": false, "error": "<msg>"}`` — never a
    5xx — so the detail page renders a degraded badge instead of an error.

    NOT a browser CORS call: the per-tenant adapter Service has no ingress and
    is only reachable from the control plane's cluster network.
    """
    session = _db(request)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    try:
        async with _new_adapter_http_client() as client:
            response = await client.get(_adapter_status_url(tenant.slug))
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — any failure → soft HTTP 200
        return {"reachable": False, "error": str(exc)}

    if isinstance(data, dict):
        data.setdefault("reachable", True)
        return data
    # Adapter returned a non-object body — still reachable, surface it raw.
    return {"reachable": True, "data": data}


# ---------------------------------------------------------------------------
# Agent issuance (L1→L2): mint a bootstrap token + return install hint.
# ---------------------------------------------------------------------------


class AgentIssuance(BaseModel):
    """Credentials + install hint the MSSP gives the tenant admin out-of-band.

    The tenant admin runs one Helm install with this payload; the agent
    self-registers against L1 (``control_plane_url``), picks up an
    ``install_helm_release`` job, and deploys ``soctalk-tenant`` into
    L2's cluster.

    Note on scope: ``agent_chart_ref`` / ``agent_chart_version`` are the
    coordinates for the ``soctalk-cloud-agent`` chart — what the tenant
    admin installs in L2. The tenant SOC stack (``soctalk-tenant``) is
    dispatched by the agent itself once registered and is not exposed
    here; it lives on the ``TenantInstallation`` row for operator
    observability via ``GET /api/mssp/tenants/{id}``.
    """

    installation_id: str
    tenant_id: str
    # Plaintext bootstrap token — shown ONCE. No recovery path.
    bootstrap_token: str
    bootstrap_expires_at: str
    # Where the L2 agent will POST /api/agent/register.
    control_plane_url: str
    # The agent chart (soctalk-cloud-agent), NOT the tenant SOC stack.
    agent_chart_ref: str
    agent_chart_version: str
    # Copy-paste ready command the MSSP admin can hand to the tenant
    # admin. One-liner; everything secret is already in the values.
    helm_install_hint: str


class RetryInstallResponse(BaseModel):
    installation_id: str
    state: str
    reclaimed: int
    reset_to_pending: int


@router.post(
    "/{tenant_id}:retry-install",
    response_model=RetryInstallResponse,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def retry_install(
    tenant_id: UUID, request: Request,
) -> RetryInstallResponse:
    """Operator-facing recovery for a wedged L2 install.

    Use when the tenant agent crashed mid-job and the installation is
    stuck in ``provisioning`` with an in_flight AgentJob. This:
      1. Reclaims any in_flight AgentJobs for the tenant's installation
         (regardless of age — operator-triggered bypasses the timeout).
      2. Resets any ``failed`` terminal jobs to ``pending`` so the agent
         can retry them on its next claim.
      3. Returns the installation's current state for the caller to poll.

    Does not re-enqueue preflight; if the failure was earlier in the
    pipeline a re-register from the agent side will reset the state.
    """
    from sqlalchemy import update as _update

    from soctalk.core.agents.models import (
        AgentJob,
        TenantInstallation,
        TenantInstallationEvent,
    )

    session = _db(request)
    installation = (
        await session.execute(
            select(TenantInstallation).where(
                TenantInstallation.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(404, "tenant has no agent installation")

    reclaimed = (
        await session.execute(
            _update(AgentJob)
            .where(AgentJob.installation_id == installation.id)
            .where(AgentJob.status == "in_flight")
            .values(status="pending", claimed_at=None)
            .returning(AgentJob.id)
        )
    ).scalars().all()

    retried = (
        await session.execute(
            _update(AgentJob)
            .where(AgentJob.installation_id == installation.id)
            .where(AgentJob.status == "failed")
            .values(
                status="pending",
                claimed_at=None,
                completed_at=None,
                outcome=None,
                error_code=None,
                summary=None,
            )
            .returning(AgentJob.id)
        )
    ).scalars().all()

    # Rebuild specs for any job being reset. The original spec is frozen
    # at queue time, which means a job that failed (or got stuck mid-claim)
    # because of stale chart_ref / chart_version / integration config would
    # retry with the same broken spec — pointless. Regenerate from current
    # source of truth (tenant_installations.desired_chart_*,
    # IntegrationConfig, etc.) so :retry actually picks up operator fixes
    # (chart version bump, external SIEM creds added, L1 URL corrected, ...).
    rebuild_ids = set(reclaimed) | set(retried)
    if rebuild_ids:
        from soctalk.core.agents.api import (
            _build_install_helm_release_spec,
            _build_wait_for_ready_spec,
        )
        rebuild_jobs = (
            await session.execute(
                select(AgentJob).where(AgentJob.id.in_(rebuild_ids))
            )
        ).scalars().all()
        for job in rebuild_jobs:
            try:
                if job.kind in ("install_helm_release", "upgrade_helm_release"):
                    job.spec = await _build_install_helm_release_spec(
                        session, installation
                    )
                elif job.kind == "wait_for_ready":
                    job.spec = await _build_wait_for_ready_spec(
                        session, installation
                    )
                # preflight + other kinds: spec is parameter-free, leave as-is.
            except Exception as exc:
                structlog.get_logger().warning(
                    "retry_install.rebuild_spec_failed",
                    job_id=str(job.id),
                    kind=job.kind,
                    error=str(exc),
                )

    actor = request.state.user_identity.get("user_id") \
        if getattr(request.state, "user_identity", None) else "operator"

    # When un-wedging a degraded install, pick the state that matches
    # the kind of job being retried. An upgrade_helm_release that
    # previously failed must resume under ``upgrading`` — the inline
    # controller's kind→state table (install→provisioning,
    # upgrade→upgrading) rejects the success otherwise and the
    # installation would hang in degraded forever.
    resume_state = "provisioning"
    if installation.state == "degraded" and retried:
        has_upgrade_failure = (
            await session.execute(
                select(AgentJob)
                .where(AgentJob.installation_id == installation.id)
                .where(AgentJob.kind == "upgrade_helm_release")
                .where(AgentJob.id.in_(retried))
            )
        ).first()
        if has_upgrade_failure is not None or installation.desired_action == "upgrade":
            resume_state = "upgrading"

    session.add(
        TenantInstallationEvent(
            installation_id=installation.id,
            event_type="retry_requested",
            from_state=installation.state,
            to_state=(
                resume_state if installation.state == "degraded" and retried
                else installation.state
            ),
            actor_id=f"user:{actor}",
            details={
                "reclaimed_in_flight": len(reclaimed),
                "retried_failed": len(retried),
                "resume_state": resume_state if retried else None,
            },
        )
    )
    if installation.state == "degraded" and retried:
        installation.state = resume_state
        installation.state_changed_at = datetime.utcnow()
    await session.commit()

    return RetryInstallResponse(
        installation_id=str(installation.id),
        state=installation.state,
        reclaimed=len(reclaimed),
        reset_to_pending=len(retried),
    )


@router.post(
    "/{tenant_id}:issue-agent",
    response_model=AgentIssuance,
    status_code=201,
    dependencies=[
        Depends(require_role(Role.PLATFORM_ADMIN, Role.MSSP_ADMIN))
    ],
)
async def issue_agent(
    tenant_id: UUID, request: Request,
) -> AgentIssuance:
    """Create-or-reuse the TenantInstallation for this tenant and mint a
    fresh bootstrap token.

    Re-calling revokes any un-consumed prior bootstrap token for the
    installation (a lost token never remains valid). Runtime tokens are
    untouched — this endpoint only addresses the bootstrap leg.
    """
    from datetime import timedelta

    from soctalk.core.agents.models import (
        TenantInstallation,
        TenantInstallationBootstrapToken,
    )
    from soctalk.core.agents.tokens import hash_token, mint_token
    from sqlalchemy import update as _update

    session = _db(request)
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    # Two chart coordinates in play:
    #
    # * ``chart_ref`` / ``chart_version`` — the tenant SOC stack
    #   (soctalk-tenant) the *agent* applies on L2 once registered.
    #   Stored on TenantInstallation.desired_* so re-issue + upgrade
    #   flows can move it atomically.
    # * ``agent_chart_ref`` / ``agent_chart_version`` — the
    #   soctalk-cloud-agent chart the *tenant admin* Helm-installs.
    #   Returned in AgentIssuance; never persisted.
    import os
    chart_ref = os.getenv(
        "SOCTALK_TENANT_CHART_REF",
        "oci://ghcr.io/soctalk/charts/soctalk-tenant",
    )
    chart_version = os.getenv("SOCTALK_TENANT_CHART_VERSION", "0.1.0")
    agent_chart_ref = os.getenv(
        "SOCTALK_AGENT_CHART_REF",
        "oci://ghcr.io/soctalk/charts/soctalk-cloud-agent",
    )
    agent_chart_version = os.getenv(
        "SOCTALK_AGENT_CHART_VERSION", "0.1.0"
    )
    control_plane_url = os.getenv(
        "SOCTALK_L1_PUBLIC_URL", "http://host.docker.internal:8000"
    )

    # Create-or-fetch the Installation row. Unique index on tenant_id
    # ensures at-most-one row per tenant.
    installation = (
        await session.execute(
            select(TenantInstallation)
            .where(TenantInstallation.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if installation is None:
        installation = TenantInstallation(
            tenant_id=tenant_id,
            desired_chart_ref=chart_ref,
            desired_chart_version=chart_version,
            desired_action="install",
            state="pending",
        )
        session.add(installation)
        await session.flush()
    else:
        # Re-issue: handle a chart bump depending on current state.
        #
        # The install_helm_release / upgrade_helm_release AgentJob spec
        # is frozen when enqueued (``agent_jobs.spec`` is a stored JSON
        # snapshot). So when the job is already in flight, updating
        # ``installation.desired_chart_version`` would desync: the
        # agent still applies the snapshotted version, but L1's later
        # ``wait_for_ready`` success copies the *current* desired into
        # ``reported_chart_version`` and we settle as if the new chart
        # was installed. Gate the update by state:
        #
        #   pending / agent_connected  → metadata update only; the
        #       install spec will be built later from the latest
        #       desired_*, so there is nothing to desync.
        #   active                     → dispatch the upgrade path.
        #   degraded                   → dispatch the upgrade path;
        #       the failed job stays as audit, a new job re-enqueues.
        #   provisioning / upgrading   → REJECT 409. Operator must
        #       wait for the current job to settle (or :retry-install
        #       then re-issue) before the chart can move.
        chart_moved = (
            installation.desired_chart_ref != chart_ref
            or installation.desired_chart_version != chart_version
        )
        if chart_moved:
            if installation.state in {"provisioning", "upgrading"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "AGENT_JOB_IN_FLIGHT",
                        "message": (
                            "chart update rejected while install/upgrade "
                            "is in flight; wait for current job to settle "
                            "or call :retry-install first"
                        ),
                        "state": installation.state,
                        "current_desired_chart_version": (
                            installation.desired_chart_version
                        ),
                        "requested_chart_version": chart_version,
                    },
                )
            prior_state = installation.state
            installation.desired_chart_ref = chart_ref
            installation.desired_chart_version = chart_version
            installation.desired_action = "upgrade"

            # Active or degraded install + new desired chart → dispatch
            # the upgrade_helm_release path. Without this,
            # desired_action="upgrade" is metadata-only: register()
            # won't re-enqueue preflight on a connected install, and
            # the inline controller only walks the first-install
            # pipeline.
            if installation.state in {"active", "degraded"}:
                from soctalk.core.agents.api import (
                    _build_install_helm_release_spec,
                    _enqueue_agent_job,
                )
                from soctalk.core.agents.models import TenantInstallationEvent

                installation.state = "upgrading"
                installation.state_changed_at = datetime.utcnow()
                session.add(
                    TenantInstallationEvent(
                        installation_id=installation.id,
                        event_type="upgrade_started",
                        from_state=prior_state,
                        to_state="upgrading",
                        actor_id="controller",
                        details={
                            "chart_ref": chart_ref,
                            "chart_version": chart_version,
                        },
                    )
                )
                await session.flush()

                # Re-use the install spec builder — same chart contract,
                # same values — and emit as an upgrade_helm_release job.
                spec = await _build_install_helm_release_spec(
                    session, installation
                )
                await _enqueue_agent_job(
                    session,
                    installation_id=installation.id,
                    kind="upgrade_helm_release",
                    idempotency_key=(
                        f"upgrade:{installation.id}:"
                        f"{chart_ref}:{chart_version}"
                    ),
                    spec=spec,
                )
            else:
                # pending / agent_connected: the install spec hasn't
                # been built yet. Metadata-only update; the builder
                # reads the latest desired_* when preflight completes.
                await session.flush()

    # Revoke any outstanding un-consumed bootstrap tokens.
    now = datetime.utcnow()
    await session.execute(
        _update(TenantInstallationBootstrapToken)
        .where(
            TenantInstallationBootstrapToken.installation_id == installation.id
        )
        .where(TenantInstallationBootstrapToken.consumed_at.is_(None))
        .where(TenantInstallationBootstrapToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )

    # Mint the new one. 24h TTL, single-use (enforced on consume).
    plaintext = mint_token()
    expires_at = now + timedelta(hours=24)
    session.add(
        TenantInstallationBootstrapToken(
            installation_id=installation.id,
            token_hash=hash_token(plaintext),
            expires_at=expires_at,
        )
    )
    await session.commit()

    # One-line install hint the MSSP admin copy-pastes to the tenant
    # admin. Values flow through --set-string so shell escaping stays
    # sane for the bootstrap token.
    install_release = f"soctalk-agent-{tenant.slug}"
    helm_install_hint = (
        f"helm install {install_release} {agent_chart_ref} "
        f"--version {agent_chart_version} "
        f"--namespace soctalk-agent --create-namespace "
        f"--set-string controlPlaneUrl={control_plane_url} "
        f"--set-string bootstrapToken={plaintext}"
    )

    return AgentIssuance(
        installation_id=str(installation.id),
        tenant_id=str(tenant_id),
        bootstrap_token=plaintext,
        bootstrap_expires_at=expires_at.isoformat(),
        control_plane_url=control_plane_url,
        agent_chart_ref=agent_chart_ref,
        agent_chart_version=agent_chart_version,
        helm_install_hint=helm_install_hint,
    )
