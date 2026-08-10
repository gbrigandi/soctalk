"""Per-tier LLM provider/base_url config loading (issue #4).

load_config builds LLMConfig.tiers from SOCTALK_<TIER>_* env so the
InferenceRequest resolver can route the high-volume router loop to a cheap
self-hosted (OpenAI-compatible) endpoint while the reasoning verdict stays on
a frontier model — without forking the config system. Backward compatibility:
single-provider deployments produce no tier overrides and keep the strict
mutual-exclusion guard.
"""

from __future__ import annotations

import pytest

from soctalk.config import load_config
from soctalk.core.llm_provider import OPENAI_SENTINEL_BASE_URL
from soctalk.inference import InferenceTier, ProviderEngine, resolve_tier

_PREFIXES = ("SOCTALK_", "ANTHROPIC_", "OPENAI_")


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all LLM env so each test starts from nothing, and load from a
    throwaway env_file so the repo .env never leaks in."""
    import os

    for k in list(os.environ):
        if k.startswith(_PREFIXES):
            monkeypatch.delenv(k, raising=False)

    def _load(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return load_config(env_file="/dev/null").llm

    return _load


# ------------------------------------------------------------- mixed routing


def test_mixed_provider_tiers_resolve(clean_env):
    cfg = clean_env(
        SOCTALK_FAST_PROVIDER="openai",
        SOCTALK_FAST_MODEL="qwen3-32b",
        SOCTALK_FAST_BASE_URL="http://sglang.internal/v1",
        SOCTALK_FAST_ENGINE="sglang",
        SOCTALK_FAST_API_KEY="sk-local-dummy",
        SOCTALK_REASONING_PROVIDER="anthropic",
        SOCTALK_REASONING_MODEL="claude-sonnet-4-6",
        ANTHROPIC_API_KEY="ak-real",
    )
    router = resolve_tier(cfg, InferenceTier.ROUTER)
    reasoning = resolve_tier(cfg, InferenceTier.REASONING)

    assert router.provider == "openai"
    assert router.engine == ProviderEngine.SGLANG
    assert router.model == "qwen3-32b"
    assert router.llm_config.openai_base_url == "http://sglang.internal/v1"
    assert router.llm_config.openai_api_key == "sk-local-dummy"
    assert router.llm_config.anthropic_api_key == ""  # scoped: other provider scrubbed

    assert reasoning.provider == "anthropic"
    assert reasoning.engine == ProviderEngine.FRONTIER
    assert reasoning.model == "claude-sonnet-4-6"
    assert reasoning.llm_config.anthropic_api_key == "ak-real"
    assert reasoning.llm_config.openai_api_key == ""


def test_per_tier_sampling_env_reaches_resolver(clean_env):
    # Full env-load path (Codex): SOCTALK_FAST_TEMPERATURE/_MAX_TOKENS must map
    # through _TIER_ENV_PREFIXES into cfg.tiers["router"] and be picked up by
    # resolve_tier_sampling — proving the fast→router rename lines up end to end.
    from soctalk.inference import resolve_tier_sampling

    cfg = clean_env(
        SOCTALK_FAST_PROVIDER="openai",
        SOCTALK_FAST_MODEL="qwen3-32b",
        SOCTALK_FAST_BASE_URL="http://sglang.internal/v1",
        SOCTALK_FAST_ENGINE="sglang",
        SOCTALK_FAST_API_KEY="sk-local",
        SOCTALK_FAST_TEMPERATURE="0.3",
        SOCTALK_FAST_MAX_TOKENS="2048",
    )
    assert cfg.tiers["router"]["temperature"] == 0.3
    assert cfg.tiers["router"]["max_tokens"] == 2048
    s = resolve_tier_sampling(cfg, InferenceTier.ROUTER, temperature=0.0, max_tokens=4096)
    assert (s.temperature, s.max_tokens) == (0.3, 2048)
    # A tier without a sampling override falls back to the caller default.
    r = resolve_tier_sampling(cfg, InferenceTier.REASONING, temperature=0.1, max_tokens=2048)
    assert (r.temperature, r.max_tokens) == (0.1, 2048)


def test_per_tier_sampling_env_out_of_range_rejected(clean_env):
    with pytest.raises(ValueError, match="out of range"):
        clean_env(
            SOCTALK_FAST_PROVIDER="openai",
            SOCTALK_FAST_BASE_URL="http://sglang:8000/v1",
            SOCTALK_FAST_MODEL="m",
            SOCTALK_FAST_TEMPERATURE="9",
        )


def test_per_tier_api_key_avoids_second_global_key(clean_env):
    # Only ANTHROPIC_API_KEY globally; the served router carries its own key,
    # so no OPENAI_API_KEY is needed and the guard is irrelevant.
    cfg = clean_env(
        SOCTALK_FAST_PROVIDER="openai",
        SOCTALK_FAST_BASE_URL="http://vllm:8000/v1",
        SOCTALK_FAST_ENGINE="vllm",
        SOCTALK_FAST_API_KEY="sk-served",
        ANTHROPIC_API_KEY="ak",
    )
    router = resolve_tier(cfg, InferenceTier.ROUTER)
    assert router.provider == "openai"
    assert router.llm_config.openai_api_key == "sk-served"


# ------------------------------------------------------------- backward compat


def test_single_provider_leaves_tiers_empty(clean_env):
    cfg = clean_env(SOCTALK_FAST_MODEL="claude-x", ANTHROPIC_API_KEY="ak")
    assert cfg.tiers == {}


def test_chat_sampling_defaults_and_env_override(clean_env):
    # Chat sampling lifted from hardcoded literals into config (#10).
    default = clean_env(ANTHROPIC_API_KEY="ak")
    assert default.chat_temperature == 0.2
    assert default.chat_max_tokens == 2048
    tuned = clean_env(
        ANTHROPIC_API_KEY="ak",
        SOCTALK_CHAT_TEMPERATURE="0.5",
        SOCTALK_CHAT_MAX_TOKENS="4096",
    )
    assert tuned.chat_temperature == 0.5
    assert tuned.chat_max_tokens == 4096


def test_model_only_tier_var_creates_no_override(clean_env):
    # A bare per-tier MODEL (no provider/base_url/engine) must not materialize a
    # tier — otherwise it would relax the mutual-exclusion guard for a plain
    # single-provider deployment.
    cfg = clean_env(
        SOCTALK_FAST_MODEL="m1",
        SOCTALK_REASONING_MODEL="m2",
        SOCTALK_CHAT_MODEL="m3",
        ANTHROPIC_API_KEY="ak",
    )
    assert cfg.tiers == {}


def test_both_keys_rejected_without_tiers(clean_env):
    with pytest.raises(ValueError, match="mutually exclusive"):
        clean_env(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o")


# --------------------------------------------------------------- mixed guard


def test_both_keys_allowed_with_tiers(clean_env):
    cfg = clean_env(
        ANTHROPIC_API_KEY="a",
        OPENAI_API_KEY="o",
        SOCTALK_FAST_PROVIDER="openai",
        SOCTALK_FAST_BASE_URL="http://vllm/v1",
        SOCTALK_REASONING_PROVIDER="anthropic",
    )
    # Global/default provider keeps the historical default when unspecified.
    assert cfg.provider == "anthropic"
    assert set(cfg.tiers) == {"router", "reasoning"}


def test_mixed_mode_respects_explicit_default_provider(clean_env):
    cfg = clean_env(
        SOCTALK_LLM_PROVIDER="openai",
        ANTHROPIC_API_KEY="a",
        OPENAI_API_KEY="o",
        SOCTALK_REASONING_PROVIDER="anthropic",
    )
    assert cfg.provider == "openai"


# ---------------------------------------------------------------- validation


def test_invalid_tier_provider_rejected(clean_env):
    with pytest.raises(ValueError, match="SOCTALK_FAST_PROVIDER"):
        clean_env(SOCTALK_FAST_PROVIDER="bedrock", SOCTALK_FAST_BASE_URL="x", ANTHROPIC_API_KEY="a")


def test_invalid_tier_engine_rejected(clean_env):
    with pytest.raises(ValueError, match="SOCTALK_FAST_ENGINE"):
        clean_env(
            SOCTALK_FAST_ENGINE="tensorrt",
            SOCTALK_FAST_BASE_URL="x",
            SOCTALK_FAST_PROVIDER="openai",
            ANTHROPIC_API_KEY="a",
        )


def test_invalid_tier_decoding_mode_rejected(clean_env):
    with pytest.raises(ValueError, match="SOCTALK_FAST_DECODING_MODE"):
        clean_env(
            SOCTALK_FAST_DECODING_MODE="regex",
            SOCTALK_FAST_PROVIDER="openai",
            SOCTALK_FAST_BASE_URL="x",
            ANTHROPIC_API_KEY="a",
        )


def test_served_engine_without_base_url_rejected(clean_env):
    # A vllm/sglang tier without a base_url would dial api.openai.com — reject
    # loudly at config time (SOCTALK_FAST_PROVIDER makes it a real tier entry).
    with pytest.raises(ValueError, match="requires SOCTALK_FAST_BASE_URL"):
        clean_env(SOCTALK_FAST_ENGINE="vllm", SOCTALK_FAST_PROVIDER="openai",
                  ANTHROPIC_API_KEY="a")


@pytest.mark.parametrize(
    "base_url",
    (
        OPENAI_SENTINEL_BASE_URL,
        "https://API.OPENAI.COM/v1",
        "https://API.OPENAI.COM/V1",
    ),
)
def test_chat_served_engine_rejects_global_openai_sentinel_base_url(
    clean_env, base_url
):
    # This is the system-chart fleet-chat failure mode: SOCTALK_CHAT_ENGINE made
    # chat price as self-hosted while OPENAI_BASE_URL still pointed at hosted OpenAI.
    with pytest.raises(ValueError, match="requires SOCTALK_CHAT_BASE_URL"):
        clean_env(
            SOCTALK_CHAT_ENGINE="sglang",
            OPENAI_BASE_URL=base_url,
            OPENAI_API_KEY="o",
        )


@pytest.mark.parametrize(
    "base_url",
    (
        OPENAI_SENTINEL_BASE_URL,
        "https://API.OPENAI.COM/v1",
        "https://API.OPENAI.COM/V1",
    ),
)
def test_served_engine_rejects_tier_openai_sentinel_base_url(clean_env, base_url):
    with pytest.raises(ValueError, match="requires SOCTALK_FAST_BASE_URL"):
        clean_env(
            SOCTALK_FAST_ENGINE="vllm",
            SOCTALK_FAST_BASE_URL=base_url,
            OPENAI_API_KEY="o",
        )


def test_served_engine_with_base_url_ok(clean_env):
    cfg = clean_env(SOCTALK_FAST_ENGINE="sglang", SOCTALK_FAST_PROVIDER="openai",
                    SOCTALK_FAST_BASE_URL="http://sglang:8000/v1", ANTHROPIC_API_KEY="a")
    assert cfg.tiers["router"]["engine"] == "sglang"


def test_served_engine_with_whitespace_padded_custom_base_url_ok(clean_env):
    cfg = clean_env(
        SOCTALK_FAST_ENGINE="sglang",
        SOCTALK_FAST_PROVIDER="openai",
        SOCTALK_FAST_BASE_URL=" http://sglang:8000/v1 ",
        ANTHROPIC_API_KEY="a",
    )
    assert cfg.tiers["router"]["base_url"] == "http://sglang:8000/v1"


def test_served_engine_accepts_global_openai_base_url(clean_env):
    # A served tier can reuse the global OPENAI_BASE_URL the resolver inherits —
    # no per-tier base_url required (Codex #4 review).
    cfg = clean_env(SOCTALK_FAST_ENGINE="vllm", OPENAI_BASE_URL="http://vllm:8000/v1",
                    ANTHROPIC_API_KEY="a")
    assert cfg.tiers["router"]["engine"] == "vllm"


@pytest.mark.parametrize(
    "base_url",
    (
        OPENAI_SENTINEL_BASE_URL,
        " https://api.openai.com/v1 ",
        "https://api.openai.com/v1/",
        "https://api.openai.com:443/v1",
        "https://API.OPENAI.COM/v1",
        "https://API.OPENAI.COM/V1",
        " https://Api.OpenAI.Com:443/v1 ",
    ),
)
def test_primary_served_engine_rejects_hosted_openai_base_url(clean_env, base_url):
    with pytest.raises(ValueError, match="requires OPENAI_BASE_URL"):
        clean_env(
            SOCTALK_LLM_PROVIDER="openai",
            SOCTALK_LLM_ENGINE="sglang",
            OPENAI_BASE_URL=base_url,
            OPENAI_API_KEY="o",
        )


def test_base_url_only_tier_does_not_relax_guard(clean_env):
    # A base_url-only override rides the global provider — it is NOT mixed-
    # provider intent, so both keys must still be rejected (Codex #4 review).
    with pytest.raises(ValueError, match="mutually exclusive"):
        clean_env(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o",
                  SOCTALK_FAST_BASE_URL="http://gw/v1")


def test_served_engine_tier_relaxes_guard(clean_env):
    # A served engine routes to the OpenAI client — genuine mixed intent, both
    # keys allowed.
    cfg = clean_env(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="o",
                    SOCTALK_FAST_ENGINE="vllm", SOCTALK_FAST_BASE_URL="http://vllm/v1")
    assert cfg.tiers["router"]["engine"] == "vllm"


def test_per_tier_keys_allow_no_global_key(clean_env):
    # Two self-hosted OpenAI-compatible tiers keyed ONLY per-tier — no global
    # OPENAI_API_KEY — must load (the fallback global key isn't used). Found by
    # the open-model combo experiment.
    cfg = clean_env(
        SOCTALK_LLM_PROVIDER="openai",
        SOCTALK_FAST_PROVIDER="openai", SOCTALK_FAST_BASE_URL="http://a:8000/v1",
        SOCTALK_FAST_API_KEY="sk-a", SOCTALK_FAST_MODEL="m-a",
        SOCTALK_REASONING_PROVIDER="openai", SOCTALK_REASONING_BASE_URL="http://b:8000/v1",
        SOCTALK_REASONING_API_KEY="sk-b", SOCTALK_REASONING_MODEL="m-b",
    )
    assert cfg.tiers["router"]["api_key"] == "sk-a"
    assert cfg.tiers["reasoning"]["api_key"] == "sk-b"


def test_single_provider_still_requires_a_key(clean_env):
    # No mixed intent → the global-key requirement stands (no silent keyless run).
    with pytest.raises(ValueError, match="No LLM API key configured"):
        clean_env(SOCTALK_LLM_PROVIDER="openai", SOCTALK_FAST_MODEL="m")
