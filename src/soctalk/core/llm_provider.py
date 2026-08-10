"""Shared LLM provider/model helpers.

Single source of truth for the provider-string canonicalization and the
provider↔model consistency rules that previously lived in two places —
``api/llm_config.LlmConfigUpdate._normalize_provider`` and the inline
``_is_openai_model`` / ``_is_anthropic_model`` closures in
``provisioning/controller._copy_llm_key_to_tenant_ns``. Both the API layer
(onboard + PATCH /llm) and the provisioning controller import from here so
the two cannot drift.

No soctalk imports — this module must stay dependency-free to avoid
circular imports between ``core.api`` and ``core.provisioning``.
"""

from __future__ import annotations

ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
OPENAI_DEFAULT_MODEL = "gpt-4o"

# The wizard prefills this OpenAI endpoint as ``llm_base_url`` even when the
# operator never opened "LLM (advanced)"; it is the ``TenantOnboard`` /
# ``IntegrationConfig`` column default too. Treated as the "unset" sentinel so
# a base_url that is still exactly this can be reconciled to the resolved
# provider (below), while any operator-customized endpoint is left untouched.
OPENAI_SENTINEL_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"

# The canonical fallback provider. The tenant chart's values.schema.json
# only admits ``openai-compatible`` or ``anthropic`` for ``llm.provider``
# (the runs-worker template maps ``openai-compatible`` → the SDK's
# ``openai`` env-side).
DEFAULT_PROVIDER = "openai-compatible"


def has_usable_served_base_url(base_url: str | None) -> bool:
    """Return true only for a non-empty custom served-engine endpoint."""
    normalized = (base_url or "").strip()
    return bool(normalized) and normalized != OPENAI_SENTINEL_BASE_URL


def normalize_provider(provider: str | None) -> str | None:
    """Canonicalize ``openai`` → ``openai-compatible`` for storage.

    Every value persisted to ``integration_configs.llm_provider`` (and
    consequently flowed into ``values.llm.provider`` at helm-render time)
    must be accepted by ``charts/soctalk-tenant/values.schema.json``, which
    only admits ``openai-compatible`` or ``anthropic``. The chart maps
    ``openai-compatible`` back to the SDK's ``openai`` provider for
    SOCTALK_LLM_PROVIDER, so functional behavior is identical — only the
    on-disk string differs. Without this normalization, storing the bare
    ``openai`` saves cleanly but the next install/upgrade for that tenant
    fails chart schema validation.
    """
    if provider == "openai":
        return DEFAULT_PROVIDER
    return provider


def is_openai_model(model: str | None) -> bool:
    """Heuristic: does the model name clearly belong to OpenAI?"""
    if not model:
        return False
    lowered = model.lower()
    return (
        lowered.startswith("gpt-")
        or lowered.startswith("o1")
        or lowered.startswith("o3")
    )


def is_anthropic_model(model: str | None) -> bool:
    """Heuristic: does the model name clearly belong to Anthropic?"""
    return model is not None and model.lower().startswith("claude")


def infer_provider_from_key(api_key: str) -> str:
    """Infer the provider from a raw API key's vendor prefix.

    ``sk-ant-`` keys are unambiguously Anthropic; everything else keeps the
    ``openai-compatible`` default (covers ``sk-``, ``sk-proj-``, and any
    OpenAI-compatible gateway credential).
    """
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    return DEFAULT_PROVIDER


def reconcile_provider_model(provider: str, model: str | None) -> str | None:
    """Flip a clearly-mismatched model to the provider's default.

    A tenant configured with ``llm.model=gpt-4o`` but switched to anthropic
    would render ``SOCTALK_FAST_MODEL=gpt-4o`` on the runs-worker, which the
    Anthropic SDK rejects on every call (and vice versa). Only overwrite the
    model when the existing one clearly belongs to the *other* provider —
    preserves operator-set custom models that already match.
    """
    if provider == "anthropic" and is_openai_model(model):
        return ANTHROPIC_DEFAULT_MODEL
    if provider in ("openai", DEFAULT_PROVIDER) and is_anthropic_model(model):
        return OPENAI_DEFAULT_MODEL
    return model


def reconcile_provider_base_url(provider: str, base_url: str) -> str:
    """Keep the primary ``llm_base_url`` consistent with the resolved provider.

    The onboard wizard prefills the OpenAI base_url. When the provider then
    resolves to ``anthropic`` — either via the install default (no key) or
    inferred from an ``sk-ant-`` key — but the base_url is still the OpenAI
    sentinel, the render derives the runs-worker egress NetworkPolicy
    (``allowedLlmHosts`` / ``llmEgressPort``) from that base_url. The policy
    would then open ``api.openai.com`` while the Anthropic client actually
    calls ``api.anthropic.com``, and the call is silently dropped. Swap the
    sentinel to the canonical Anthropic endpoint so the egress host matches
    the provider. Any operator-customized base_url (a proxy, a self-hosted
    gateway) is preserved verbatim — only the exact unset sentinel is flipped.
    """
    if provider == "anthropic" and base_url == OPENAI_SENTINEL_BASE_URL:
        return ANTHROPIC_DEFAULT_BASE_URL
    return base_url
