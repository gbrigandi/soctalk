"""Shared LLM provider/model helpers.

Single source of truth for the provider-string canonicalization and the
provider↔model consistency rules that previously lived in two places —
``api/llm_config.LlmConfigUpdate._normalize_provider`` and the inline
``_is_openai_model`` / ``_is_anthropic_model`` closures in
``provisioning/controller._copy_llm_key_to_tenant_ns``. Both the API layer
(onboard + PATCH /llm) and the provisioning controller import from here so
the two cannot drift.

Keep any heavier soctalk imports lazy: ``core.tenancy.models`` imports this
module, while the pricing classifier imports tenancy models for its runtime
paths. A top-level import would create a cycle.
"""

from __future__ import annotations

from urllib.parse import urlparse

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

_HTTPX_DOT_FOLD = str.maketrans({
    "\u3002": ".",
    "\uff0e": ".",
    "\uff61": ".",
})

# Hosted authorities whose billing/provider identity wins over any stale served
# engine claim. Keep this low-level so pricing, runtime config, and write guards
# all ask the same table.
HOSTED_LLM_AUTHORITIES: dict[str, str] = {
    "openrouter.ai": "openrouter",
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
}

SERVED_LLM_ENGINES = frozenset({"openai_compatible", "vllm", "sglang"})


def authority_host_for_base_url(base_url: str | None) -> str:
    """Hostname normalized the same way httpx normalizes dot equivalents.

    ``httpx`` folds the IDNA dot characters U+3002, U+FF0E, and U+FF61 to
    ``.`` before connecting, but it does not decode ``%2e`` inside the
    authority. DNS treats one trailing root dot as the same absolute host, so
    strip exactly one after folding. Keep that exact boundary so hosted-vendor
    checks agree with the actual request destination.
    """
    host = urlparse(base_url or "").hostname or ""
    host = host.translate(_HTTPX_DOT_FOLD).lower()
    return host[:-1] if host.endswith(".") else host


def _host_matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def hosted_provider_kind_for_base_url(base_url: str | None) -> str | None:
    host = authority_host_for_base_url(base_url)
    for domain, provider_kind in HOSTED_LLM_AUTHORITIES.items():
        if _host_matches_domain(host, domain):
            return provider_kind
    return None


def effective_llm_engine(
    provider: str | None, engine: str | None, base_url: str | None
) -> str | None:
    """Return the engine SocTalk should actually render/run/price.

    Historical rows can carry ``engine=sglang`` or ``vllm`` while their base URL
    points at a hosted authority. The request still goes to that hosted service,
    so the served-engine claim is inert and must not be inherited by tiers or
    pricing. Validation still rejects new writes before they reach storage.
    """
    normalized = (engine or "").strip().lower() or None
    if normalized in SERVED_LLM_ENGINES and (provider or "").strip().lower() in {
        "openai",
        DEFAULT_PROVIDER,
    }:
        if hosted_provider_kind_for_base_url(base_url) is not None:
            return None
    return normalized


def has_usable_served_base_url(base_url: str | None) -> bool:
    """Return true only for a non-empty custom served-engine endpoint.

    Hosted vendor authorities are not usable for vLLM/SGLang: the
    request would still go to the vendor API while pricing calls it self-hosted.
    Reuse the same authority table as pricing so runtime validation and cost
    classification cannot drift.
    """
    normalized = (base_url or "").strip()
    if not normalized:
        return False
    if not urlparse(normalized).hostname:
        return False
    return hosted_provider_kind_for_base_url(normalized) is None


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
