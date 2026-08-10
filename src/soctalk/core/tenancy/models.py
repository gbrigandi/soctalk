"""V1 multi-tenancy SQLModel definitions.

``docs/multi-tenant/security-model.md``.

Design notes
------------
- Shared-database tenancy with ``tenant_id`` on every tenant-scoped table.
- Row-Level Security enforced at Postgres layer (see ``docs/multi-tenant/postgres-rls.md``);
  these Python models declare the schema but rely on the migration to attach
  ``ENABLE ROW LEVEL SECURITY`` and ``FORCE ROW LEVEL SECURITY``.
- ``TenantSecret`` stores *references only*. ``(namespace, secret_name, version_label)``.
  Raw secret material lives in Kubernetes Secrets (see secret-placement).
- ``User`` carries both MSSP-side staff (``tenant_id`` NULL) and customer-side
  users (``tenant_id`` set, role = ``customer_viewer``). The RLS policy allows
  NULL-tenant rows to be visible across contexts for join-style access from
  MSSP endpoints (see postgres-rls §5.2).

Roles follow a 4-role model:
``platform_admin``, ``mssp_admin``, ``analyst``, ``customer_viewer``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Column, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, Text

from soctalk.core.llm_provider import (
    OPENAI_SENTINEL_BASE_URL,
    has_usable_served_base_url,
)


class Role(str, Enum):
    """Roles across three functional tiers per audience (see
    ``soctalk.core.tenancy.permissions`` for the capability bundles):

      MSSP:   platform_admin (super) · mssp_admin (configure) ·
              mssp_manager (authorize risk) · analyst (operate)
      tenant: tenant_admin (configure) · tenant_manager (authorize risk,
              e.g. declare their own pentest engagements) ·
              tenant_analyst (operate their own SOC: triage/review/decide/chat) ·
              customer_viewer (read-only stakeholder)

    ``mssp_manager`` holds the "authorize risk" capabilities (engagements,
    authorization facts, privileged proposal sign-off) that used to sit
    with every MSSP role. ``tenant_analyst`` is the tenant-audience mirror of
    ``analyst`` — full read+write authority over its OWN tenant's SOC (co-managed
    SOC), RLS-scoped like every other tenant role. Capability checks go through
    ``require_permission``; do not add new inline role-set checks.
    """

    PLATFORM_ADMIN = "platform_admin"
    MSSP_ADMIN = "mssp_admin"
    MSSP_MANAGER = "mssp_manager"
    ANALYST = "analyst"
    TENANT_ADMIN = "tenant_admin"
    TENANT_MANAGER = "tenant_manager"
    TENANT_ANALYST = "tenant_analyst"
    CUSTOMER_VIEWER = "customer_viewer"


class UserType(str, Enum):
    """Broad user category.

    ``mssp`` covers platform_admin / mssp_admin / analyst (cross-tenant).
    ``tenant`` covers customer_viewer (tenant-scoped).
    Worker and adapter principals do not sit in the user table.
    """

    MSSP = "mssp"
    TENANT = "tenant"


class TenantState(str, Enum):
    """Tenant lifecycle states (see docs/multi-tenant/two-chart-contract §6)."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEGRADED = "degraded"
    DECOMMISSIONING = "decommissioning"
    ARCHIVED = "archived"
    PURGED = "purged"


class AuditAction(str, Enum):
    """Canonical audit action identifiers (non-exhaustive; extend as needed)."""

    TENANT_CREATE = "tenant.create"
    TENANT_SUSPEND = "tenant.suspend"
    TENANT_RESUME = "tenant.resume"
    TENANT_DECOMMISSION = "tenant.decommission"
    TENANT_CONFIG_UPDATE = "tenant.config.update"
    TENANT_BRANDING_UPDATE = "tenant.branding.update"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_IMPERSONATE = "user.impersonate"
    SETTINGS_UPDATE = "settings.update"
    INVESTIGATION_APPROVE = "investigation.approve"
    INVESTIGATION_REJECT = "investigation.reject"
    SYSTEM_CONTEXT_ENTER = "system.context.enter"
    LICENSE_ROTATE = "license.rotate"


# ----------------------------------------------------------------------------
# Install-scoped tables (no RLS)
# ----------------------------------------------------------------------------


class Organization(SQLModel, table=True):
    """Install-level identity.

    One row per install. MSSP identity + install identity recorded here. Values
    come from the license JWT (V1.5) or from chart values at install time (V1).
    """

    __tablename__ = "organizations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    mssp_id: UUID = Field(unique=True, index=True)
    mssp_name: str = Field(max_length=255)
    install_id: UUID = Field(unique=True, index=True)
    install_label: str | None = Field(default=None, max_length=255)
    slug: str = Field(max_length=63, unique=True, index=True)
    # MSSP-level branding for slug-driven landing.
    logo_url: str | None = Field(default=None, max_length=500)
    primary_color: str | None = Field(default=None, max_length=16)
    secondary_color: str | None = Field(default=None, max_length=16)
    favicon_url: str | None = Field(default=None, max_length=500)
    # Reserved for V1.5 license JWT storage; NULL in V1.
    license_jwt: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ----------------------------------------------------------------------------
# Tenant table (install-scoped by presence, but schema defines the primary
# scoping entity for the rest of the tenant-scoped tables)
# ----------------------------------------------------------------------------


class Tenant(SQLModel, table=True):
    """End-customer tenant. One per customer per install."""

    __tablename__ = "tenants"
    __table_args__ = (
        Index("ix_tenants_slug", "slug", unique=True),
        Index("ix_tenants_state", "state"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    slug: str = Field(max_length=63)
    display_name: str = Field(max_length=255)
    state: str = Field(default=TenantState.PENDING.value, max_length=32)
    # Deployment profile: 'poc' (ephemeral), 'persistent' (single-node
    # durable), 'provided' (tenant brings their own external Wazuh; SocTalk
    # only deploys the adapter + runs-worker), or 'legacy' (pre-wizard
    # tenants — don't re-derive).
    profile: str = Field(default="poc", max_length=16)
    # Association back to the install's Organization row.
    organization_id: UUID = Field(
        sa_column=Column(ForeignKey("organizations.id"), nullable=False, index=True)
    )
    # Tracking fields
    created_at: datetime = Field(default_factory=datetime.utcnow)
    state_changed_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: datetime | None = Field(default=None)
    # Config snapshot (mirrors the tenant chart values schema; see two-chart-contract §3).
    # Stored as JSONB for schema flexibility during V1. Typed pydantic model
    # validates before writes in the application layer.
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # Runtime state reported by the adapter.
    runtime: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )


# ----------------------------------------------------------------------------
# Users (install-scoped presence with optional tenant binding)
# ----------------------------------------------------------------------------


class User(SQLModel, table=True):
    """Unified user table. MSSP-side: tenant_id NULL. Tenant-side: tenant_id set.

    Authentication happens at ingress (OIDC); this table holds the identity
    SocTalk trusts after the handoff. Role + tenant_id together determine scope.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_tenant_id", "tenant_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(max_length=320)  # RFC 5321 max length
    display_name: str | None = Field(default=None, max_length=255)
    user_type: str = Field(max_length=16)  # UserType enum value
    role: str = Field(max_length=32)  # Role enum value
    tenant_id: UUID | None = Field(
        default=None,
        sa_column=Column(ForeignKey("tenants.id"), nullable=True),
    )
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime | None = Field(default=None)


# ----------------------------------------------------------------------------
# Per-tenant config tables
# ----------------------------------------------------------------------------


# Per-tier LLM backend for a hybrid tenant (issue #12) — the deployment side of
# the runtime per-tier providers (#4). Tenant-facing ``provider`` uses the
# install enum ('openai-compatible' | 'anthropic'); render canonicalizes it to
# the runtime 'openai' | 'anthropic' and emits SOCTALK_<TIER>_* env. The tier
# key names map to runtime tiers via SOCTALK_<TIER>_*: ``fast`` (high-volume
# router loop → 'router') and ``reasoning`` (verdict). NOTE: ``chat`` and
# ``extraction`` are deliberately NOT allowed yet — the chat agent runs in the
# API process off the install env (not the tenant worker), and the extraction
# step reuses the reasoning tier, so a per-tenant chat/extraction tier would be
# a silent no-op until those call sites route per-tenant (tracked for a later
# slice; see the UI-config review).
_ALLOWED_LLM_TIERS: frozenset[str] = frozenset({"fast", "reasoning"})


ALLOWED_LLM_ENGINES = frozenset(
    {"frontier", "openai_compatible", "vllm", "sglang"}
)
SERVED_ENGINES = frozenset({"openai_compatible", "vllm", "sglang"})


def normalize_llm_engine(engine: str | None) -> str | None:
    """Canonicalize and allowlist a primary/tier LLM engine string.

    API models call this before writes. Provisioning paths call
    :func:`check_engine_provider_combo` against stored rows because
    ``IntegrationConfig`` is ``table=True`` and validators do not run there.
    """
    if engine is None:
        return None
    normalized = engine.strip().lower()
    if not normalized:
        return None
    if normalized not in ALLOWED_LLM_ENGINES:
        raise ValueError(
            f"engine must be one of {sorted(ALLOWED_LLM_ENGINES)}"
        )
    return normalized


def check_engine_provider_combo(provider: str | None, engine: str | None) -> None:
    """Reject an engine that contradicts its provider.

    ``provider_kind_for`` lets the engine win over the provider, so
    provider=anthropic with engine=sglang prices an Anthropic backend as
    self_hosted and bills it against a catalog entry it never touches
    (#142, Codex round 14). Tiers already enforced this; the primary config
    accepted it, which is how the combo reached pricing at all.

    Raises ValueError, so it reads as a 422 at the API boundary. Kept as a
    function rather than a validator because IntegrationConfig is a
    ``table=True`` SQLModel and pydantic validators do not run on those.
    """
    normalized = normalize_llm_engine(engine)
    if provider == "anthropic" and normalized in SERVED_ENGINES:
        raise ValueError(
            f"engine {normalized!r} is OpenAI-compatible; "
            "not valid with provider 'anthropic'"
        )


def check_primary_llm_engine_config(
    provider: str | None, engine: str | None, base_url: str | None
) -> None:
    """Reject a primary LLM engine config the tenant chart cannot render.

    The primary chart block suppresses OPENAI_BASE_URL when ``llm.baseUrl`` is
    absent or still the hosted OpenAI sentinel. Served/gateway engines have no
    public default endpoint, so that stored pair would render a runs-worker that
    fails ``load_config()`` at startup. Keep this outside ``IntegrationConfig``
    validators because ``table=True`` SQLModel validators do not run for DB rows.
    """
    normalized = normalize_llm_engine(engine)
    check_engine_provider_combo(provider, normalized)
    url = (base_url or "").strip()
    if normalized in SERVED_ENGINES and not has_usable_served_base_url(url):
        raise ValueError(
            f"engine {normalized!r} requires a custom llm_base_url; "
            f"{OPENAI_SENTINEL_BASE_URL!r} is the hosted OpenAI default and "
            "the tenant chart does not render OPENAI_BASE_URL for it"
        )


class LLMTierConfig(BaseModel):
    """Validated per-tier LLM backend. Stored (as a dict) in
    ``IntegrationConfig.llm_tiers`` JSONB; validated in the application layer
    before writes, matching the ``config``/``runtime`` JSONB convention."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai-compatible", "anthropic"]
    base_url: str
    model: str
    engine: Literal["frontier", "openai_compatible", "vllm", "sglang"] | None = None
    # Structured-decoding mechanism (#32). Needed for endpoints whose strict
    # json_schema/tool_choice are unavailable — e.g. DeepSeek's hosted thinking
    # models require ``json_object``. Omit to let the resolver pick per provider.
    decoding_mode: Literal[
        "auto", "none", "tool_use", "json_schema_strict", "json_object",
        "guided_json", "guided_grammar",
    ] | None = None
    # Per-tier sampling override. Omit to inherit the tier's caller default
    # (router → the tenant-global temperature/max_tokens; reasoning → the
    # verdict's tuned constants). Bounds mirror the global sampling knobs +
    # LLMConfig so the UI can't persist a value the worker would reject.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    # Own credential for a different-provider tier. Omit to reuse the primary
    # ``llm_api_key_plain`` (only valid when this tier's provider matches the
    # primary provider). Same plaintext-at-rest caveat as ``llm_api_key_plain``.
    api_key_plain: str | None = None

    @field_validator("temperature", "max_tokens", mode="before")
    @classmethod
    def _reject_bool_sampling(cls, v):
        # Pydantic v2 coerces bool → 0/1 for numeric fields; reject it for parity
        # with the global sampling API (Codex) — True must not become 1.0 / 1.
        if isinstance(v, bool):
            raise ValueError("must be a number, not a boolean")
        return v

    @field_validator("engine", mode="before")
    @classmethod
    def _normalize_engine(cls, v: str | None) -> str | None:
        return normalize_llm_engine(v)

    @field_validator("base_url")
    @classmethod
    def _trim_base_url(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_combo(self) -> LLMTierConfig:
        # Match the env resolver's rules so the UI can't persist a combo the
        # runtime would reject (or silently mishandle) at call time.
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if self.provider == "anthropic":
            if self.engine in SERVED_ENGINES:
                raise ValueError(f"engine {self.engine!r} is OpenAI-compatible; "
                                 "not valid with provider 'anthropic'")
            # Anthropic has no JSON-mode response_format, so json_object can't be
            # honoured. json_schema_strict IS accepted — the runtime resolver maps
            # it to tool_use on Anthropic (resolve_decoding_mode). guided_* is
            # caught by the served-engine rule below (Anthropic never runs on
            # vllm/sglang).
            if self.decoding_mode == "json_object":
                raise ValueError("decoding_mode 'json_object' is not available on "
                                 "Anthropic (use tool_use, json_schema_strict, or auto)")
        # Guided decoding is engine-native shaping only vLLM/SGLang implement
        # (guided_request_kwargs). frontier, openai_compatible, and an unset
        # engine (which the runtime resolves as frontier) all reject it — mirror
        # that here so the UI can't persist a combo the worker fails on.
        if self.decoding_mode in ("guided_json", "guided_grammar") and \
                self.engine not in ("vllm", "sglang"):
            raise ValueError(f"decoding_mode {self.decoding_mode!r} needs a served "
                             f"engine (vllm/sglang), not {self.engine or 'unset'!r}")
        if self.engine in SERVED_ENGINES and not has_usable_served_base_url(
            self.base_url
        ):
            raise ValueError(
                f"engine {self.engine!r} requires a custom base_url; hosted "
                "vendor API endpoints are not valid served/gateway backends"
            )
        return self


def validate_llm_tiers(raw: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    """Validate an ``llm_tiers`` payload, returning the normalized dict (or None).

    Raises ``ValueError`` on an unknown tier key or an invalid tier block, so a
    bad provisioning request fails loudly rather than persisting garbage.
    """
    if not raw:
        return None
    unknown = set(raw) - _ALLOWED_LLM_TIERS
    if unknown:
        # Never echo the raw unknown keys — a malformed client could pass a
        # pasted secret as a tier KEY, which would then leak into the API 422
        # and the UI toast. Report the count + the allowed set instead.
        raise ValueError(
            f"unknown llm_tiers key(s): {len(unknown)} not in allowed tiers "
            f"{sorted(_ALLOWED_LLM_TIERS)}"
        )
    out: dict[str, dict[str, Any]] = {}
    for tier, block in raw.items():
        try:
            out[tier] = LLMTierConfig(**block).model_dump(exclude_none=True)
        except PydanticValidationError as e:
            # Build a message from field + msg only — NEVER the input value, so a
            # bad tier block can't echo api_key_plain into an API error / UI toast.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'block'}: {err['msg']}"
                for err in e.errors()
            )
            raise ValueError(f"tier {tier!r}: {problems}") from None
    return out


def validate_llm_model_prices(
    raw: dict[str, Any] | None,
) -> dict[str, dict[str, float]] | None:
    """Validate an ``llm_model_prices`` payload, returning it normalized (or None).

    Two key shapes are accepted, in USD per million tokens:

    * ``"model"`` — applies to that model wherever the tenant runs it.
    * ``"<provider_kind>:<provider_id|*>:<model>"`` — qualified, so the same
      model string can carry different prices at different backends (#141
      phase 3). A tenant with per-tier providers genuinely has two prices for
      one name, and the bare form collapsed them.

    The bare form still matches what ``graph.budget`` parses out of
    ``SOCTALK_MODEL_PRICES``, so a value accepted here cannot be rejected by
    the worker at startup.

    Raises ``ValueError`` on a malformed entry rather than persisting it: a price
    that silently fails to parse leaves the tenant on the fail-expensive fallback,
    which is the exact failure this config exists to prevent (issue #121).
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("llm_model_prices must be an object keyed by model")
    out: dict[str, dict[str, float]] = {}
    for model, block in raw.items():
        # Never echo the key or the block in an error: a malformed client could
        # pass pasted credential material as a model KEY, and the message lands
        # in an API 422 and a UI toast. Report position and reason only.
        if not isinstance(model, str) or not model.strip():
            raise ValueError("llm_model_prices has an empty model key")
        if not isinstance(block, dict) or "input" not in block or "output" not in block:
            raise ValueError(
                f"llm_model_prices entry {len(out) + 1} must carry 'input' and 'output'"
            )
        priced: dict[str, float] = {}
        for field in ("input", "output"):
            raw_value = block[field]
            # bool is a subclass of int, so float(True) is 1.0 and would store a
            # price of $1/Mtok from a JSON `true`. Reject it as the wrong type
            # rather than coercing (Codex review round 2, finding 2).
            if isinstance(raw_value, bool):
                raise ValueError(
                    f"llm_model_prices entry {len(out) + 1}: {field} must be a number"
                )
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"llm_model_prices entry {len(out) + 1}: {field} must be a number"
                ) from None
            # NaN and infinity survive float() and would reach JSONB, where they
            # are not representable, and from there a price snapshot. NaN also
            # defeats every comparison downstream, so a run priced with one can
            # never halt.
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(
                    f"llm_model_prices entry {len(out) + 1}: {field} must be finite"
                )
            if value < 0:
                raise ValueError(
                    f"llm_model_prices entry {len(out) + 1}: {field} must not be negative"
                )
            priced[field] = value
        key = model.strip()
        # Keys are normalized by stripping, so "gpt-4o" and " gpt-4o " collapse
        # to the same model. Silently keeping the last one would mis-price a
        # tenant while the caller believes both entries took effect
        # (Codex review round 3, finding 3).
        if key in out:
            raise ValueError(
                f"llm_model_prices entry {len(out) + 1} duplicates an earlier model"
            )
        out[key] = priced
    return out


class IntegrationConfig(SQLModel, table=True):
    """Per-tenant integration endpoints (Wazuh URL, TheHive URL, etc.).

    Secret material (API keys, tokens) is *not* here: stored in K8s Secrets;
    references live in :class:`TenantSecret`.

    Replaces the single-row ``UserSettings('default')`` legacy table.
    """

    __tablename__ = "integration_configs"
    __table_args__ = (
        Index("ix_integration_configs_tenant", "tenant_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id"), nullable=False, index=True)
    )
    # Wazuh
    wazuh_enabled: bool = Field(default=True)
    wazuh_url: str | None = Field(default=None, max_length=500)
    # Indexer Service is distinct from the Manager Service in the wazuh
    # chart (wazuh-<slug>-wazuh-indexer:9200 vs ...-wazuh-manager:55000).
    # NULL ⇒ chat resolver derives by substituting -wazuh-manager →
    # -wazuh-indexer and :55000 → :9200. See docs/mssp-chat-plan.md.
    wazuh_indexer_url: str | None = Field(default=None, max_length=500)
    wazuh_verify_ssl: bool = Field(default=True)
    # External Wazuh credentials (populated only for the ``provided`` profile,
    # where the tenant brings their own Wazuh deployment rather than having
    # SocTalk provision one in-namespace). The Wazuh **API** (manager, :55000)
    # and the **Indexer** (OpenSearch, :9200) authenticate with *separate*
    # credentials, mirroring the in-cluster chart's ``credentials`` block and
    # the 4-key ``*-wazuh-creds`` Secret (WAZUH_API_USERNAME/PASSWORD +
    # INDEXER_USERNAME/PASSWORD). Plaintext storage mirrors the existing
    # ``llm_api_key_plain`` compromise; KMS/Fernet hardening is tracked as a
    # cross-column follow-up.
    #
    # Wazuh API (manager) credentials + endpoint:
    wazuh_username: str | None = Field(default=None, max_length=255)
    wazuh_password_plain: str | None = Field(default=None, max_length=4096)
    wazuh_api_token_plain: str | None = Field(default=None, max_length=4096)
    wazuh_api_url: str | None = Field(default=None, max_length=500)
    # Wazuh Indexer (OpenSearch) credentials. The indexer URL is declared
    # above (``wazuh_indexer_url``, with the in-cluster resolver default);
    # only the separate indexer credentials are added here.
    wazuh_indexer_username: str | None = Field(default=None, max_length=255)
    wazuh_indexer_password_plain: str | None = Field(default=None, max_length=4096)
    # TheHive
    thehive_enabled: bool = Field(default=True)
    thehive_url: str | None = Field(default=None, max_length=500)
    thehive_organisation: str | None = Field(default=None, max_length=255)
    thehive_verify_ssl: bool = Field(default=True)
    # Cortex
    cortex_enabled: bool = Field(default=True)
    cortex_url: str | None = Field(default=None, max_length=500)
    cortex_verify_ssl: bool = Field(default=True)
    # MISP (V1.5)
    misp_enabled: bool = Field(default=False)
    misp_url: str | None = Field(default=None, max_length=500)
    misp_verify_ssl: bool = Field(default=True)
    # Slack notifications
    slack_enabled: bool = Field(default=False)
    slack_channel: str | None = Field(default=None, max_length=100)
    slack_notify_on_escalation: bool = Field(default=True)
    slack_notify_on_verdict: bool = Field(default=True)
    # LLM config (per-tenant BYO)
    llm_provider: str = Field(default="openai-compatible", max_length=32)
    llm_base_url: str = Field(default="https://api.openai.com/v1", max_length=500)
    llm_model: str = Field(default="gpt-4o", max_length=255)
    llm_fast_model: str | None = Field(default=None, max_length=255)
    llm_reasoning_model: str | None = Field(default=None, max_length=255)
    # Serving engine behind llm_base_url, when it is one SocTalk prices
    # separately ("vllm"/"sglang"). NULL = a hosted vendor or a generic
    # gateway, which is today's behaviour.
    #
    # Pricing cannot tell a served endpoint from a gateway by host alone, so
    # without this a self-hosted install seeded tenants that priced as
    # openai_compatible and missed their own self_hosted catalog entry — the
    # model was called but never priced (#142, Codex round 13). Tiers already
    # carried an engine; the primary config had no equivalent field.
    llm_engine: str | None = Field(default=None, max_length=32)
    # Optional per-tier LLM backends for a hybrid tenant (issue #12). NULL =
    # single-provider (today's behaviour, unchanged). Shape:
    # ``{"fast": {provider, base_url, model, engine?, api_key_plain?}, "reasoning": {...}}``
    # validated by ``validate_llm_tiers`` before writes.
    llm_tiers: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    llm_temperature: float = Field(default=0.0)
    llm_max_tokens: int = Field(default=4096)
    # Per-tenant case-run budget caps (issue #5). NULL = use the worker default
    # (SOCTALK_CASE_RUN_*_BUDGET env or the built-in $5 / 15k). Enforced in
    # graph/budget.py (over_budget → supervisor CLOSE); rendered into the
    # runs-worker env only when set.
    llm_dollar_budget_per_run: float | None = Field(default=None)
    llm_token_budget_per_run: int | None = Field(default=None)
    # Per-tenant price overlay for models absent from the built-in table
    # (issue #121). NULL = no overlay, so an unpriced model falls back to the
    # fail-expensive $15/$75 rate, which is right for an unknown hosted API and
    # actively wrong for the cheap or self-hosted backends the BYO-LLM config
    # exists to point at. Shape ``{"model-prefix": {"input": x, "output": y}}``
    # in USD per million tokens, validated by ``validate_llm_model_prices``.
    # Rendered into the runs-worker env as SOCTALK_MODEL_PRICES only when set.
    llm_model_prices: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Plaintext LLM API key material stored in Postgres.
    #
    # MVP path: needed for the cross-cluster L1→L2 deploy, where the
    # L1 Postgres is the only place L1 can read the key when building
    # the install_helm_release spec (the L2 chart then materializes a
    # K8s Secret from the plaintext Helm value). The legacy in-cluster
    # path also writes the value to the ``soctalk-system/tenant-<id>-llm``
    # K8s Secret for collapsed-tier deploys.
    #
    # Production hardening: layer Fernet-at-rest or a KMS-backed column
    # here before exposing to anything other than internal operators.
    # Tracked as a follow-up to the MVP slice.
    llm_api_key_plain: str | None = Field(default=None, max_length=4096)
    # Timestamps
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class BrandingConfig(SQLModel, table=True):
    """Per-tenant branding (consumed by both UIs via config API)."""

    __tablename__ = "branding_configs"
    __table_args__ = (
        Index("ix_branding_configs_tenant", "tenant_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id"), nullable=False, index=True)
    )
    app_name: str = Field(default="SocTalk", max_length=255)
    logo_url: str | None = Field(default=None, max_length=500)
    primary_color: str | None = Field(default=None, max_length=16)
    secondary_color: str | None = Field(default=None, max_length=16)
    favicon_url: str | None = Field(default=None, max_length=500)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TenantSecret(SQLModel, table=True):
    """Reference (not material) to a Kubernetes Secret holding per-tenant credentials.

    Stored references only; actual secret material never lands in Postgres.
    See secret-placement §1.
    """

    __tablename__ = "tenant_secrets"
    __table_args__ = (
        Index("ix_tenant_secrets_tenant_purpose", "tenant_id", "purpose"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id"), nullable=False)
    )
    # Purpose enum string, e.g. "llm", "wazuh", "thehive", "cortex", "adapter-jwt"
    purpose: str = Field(max_length=64)
    k8s_namespace: str = Field(max_length=253)
    k8s_secret_name: str = Field(max_length=253)
    k8s_secret_key: str = Field(max_length=253, default="api_key")
    version_label: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    rotated_at: datetime | None = Field(default=None)


# ----------------------------------------------------------------------------
# Audit + lifecycle (tenant-scoped with nullable tenant for install-level events)
# ----------------------------------------------------------------------------


class AuditLog(SQLModel, table=True):
    """Append-only audit trail.

    ``tenant_id`` NULL means the event was install-scoped (e.g., platform_admin
    action, system context entry); set means tenant-specific. Customer users
    can read rows WHERE ``tenant_id = own_tenant``: including MSSP-impersonation
    entries where ``acting_as`` is populated.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_audit_log_actor", "actor_id"),
        Index("ix_audit_log_action", "action"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    tenant_id: UUID | None = Field(
        default=None, sa_column=Column(ForeignKey("tenants.id"), nullable=True)
    )
    actor_principal: str = Field(max_length=32)  # user | worker | system | adapter
    actor_id: str = Field(max_length=128)  # user_id | "worker:<job>" | "system:<reason>" | tenant_id
    # For impersonation: the MSSP user acting on behalf of a tenant.
    acting_as: UUID | None = Field(default=None)
    action: str = Field(max_length=64)
    resource_type: str | None = Field(default=None, max_length=64)
    resource_id: str | None = Field(default=None, max_length=128)
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    request_id: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, sa_column=Column(Text))


class TenantLifecycleEvent(SQLModel, table=True):
    """Append-only log of tenant state transitions and config revisions."""

    __tablename__ = "tenant_lifecycle_events"
    __table_args__ = (
        Index("ix_tle_tenant_ts", "tenant_id", "timestamp"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id"), nullable=False)
    )
    event_type: str = Field(max_length=64)
    # e.g., "provisioning_started", "active", "config_updated", "upgrade_requested",
    # "suspended", "resumed", "decommission_started", "archived", "purged"
    from_state: str | None = Field(default=None, max_length=32)
    to_state: str | None = Field(default=None, max_length=32)
    actor_id: str | None = Field(default=None, max_length=128)
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))


class ProvisioningJob(SQLModel, table=True):
    """Async provisioning work queue.

    One row per pending/in-flight provision-or-decommission request. Worker
    claims with ``SELECT ... FOR UPDATE SKIP LOCKED``, runs the stepwise
    reconcile on ``TenantController``, updates status + attempts.

    The partial unique index ``uq_provisioning_jobs_active`` keeps at most
    one active (pending or in_flight) job per (tenant, kind).
    """

    __tablename__ = "provisioning_jobs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(
        sa_column=Column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    )
    kind: str = Field(max_length=32)
    # 'tenant.provision' | 'tenant.reconcile' | 'tenant.decommission'
    status: str = Field(default="pending", max_length=16)
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=5)
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    claimed_at: datetime | None = Field(default=None)
    claimed_by: str | None = Field(default=None, max_length=128)
    next_attempt_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


__all__ = [
    "AuditAction",
    "AuditLog",
    "BrandingConfig",
    "IntegrationConfig",
    "Organization",
    "ProvisioningJob",
    "Role",
    "Tenant",
    "TenantLifecycleEvent",
    "TenantSecret",
    "TenantState",
    "User",
    "UserType",
]
