"""Per-tier LLM provisioning: schema validation + render (issue #12).

The deployment half of #4. A hybrid tenant stores per-tier LLM backends in
``IntegrationConfig.llm_tiers``; render turns them into ``values.llm.tiers`` +
per-tier Secret keys + a port-union egress. Single-provider tenants (llm_tiers
NULL) must render byte-identically — asserted here at the values-dict level.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from soctalk.core.api.llm_config import (
    _cross_provider_tiers_without_key,
    _merge_tier_keys,
    _sanitize_tiers,
)
from soctalk.core.provisioning.controller import _llm_secret_data
from soctalk.core.provisioning.render import render_tenant_values
from soctalk.core.tenancy.models import (
    BrandingConfig,
    IntegrationConfig,
    Tenant,
    TenantState,
    validate_llm_tiers,
)


def _tenant():
    return Tenant(id=uuid4(), slug="acme", display_name="Acme", profile="poc",
                  state=TenantState.PROVISIONING.value, organization_id=uuid4())


def _integration(tid, **over):
    base = dict(tenant_id=tid, llm_provider="anthropic",
                llm_base_url="https://api.anthropic.com", llm_model="claude-sonnet-4-6",
                llm_api_key_plain="ak-primary")
    base.update(over)
    return IntegrationConfig(**base)


def _render(integration, *, include_llm_api_key=True):
    t = _tenant()
    return render_tenant_values(
        tenant=t, integration=integration,
        branding=BrandingConfig(tenant_id=t.id, app_name="Acme SOC"),
        mssp_id=str(uuid4()), install_id=str(uuid4()),
        llm_secret_name="tenant-llm-key", include_llm_api_key=include_llm_api_key,
        profile="poc",
    )


_FAST = {"provider": "openai-compatible", "base_url": "http://sglang.internal:8000/v1",
         "model": "qwen3-32b", "engine": "sglang", "api_key_plain": "sk-served"}


# ---------------------------------------------------------------- validation


def test_install_default_fast_tier_from_env(monkeypatch):
    # Install-wide default FAST tier (demo: Qwen router). Env → validated
    # llm_tiers; unconfigured → None (no behaviour change for other installs).
    from soctalk.core.api.tenants import _install_default_llm_tiers

    for k in ("PROVIDER", "BASE_URL", "MODEL", "ENGINE", "DECODING_MODE", "API_KEY"):
        monkeypatch.delenv(f"SOCTALK_DEFAULT_FAST_TIER_{k}", raising=False)
    assert _install_default_llm_tiers() is None  # unconfigured

    monkeypatch.setenv("SOCTALK_DEFAULT_FAST_TIER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("SOCTALK_DEFAULT_FAST_TIER_BASE_URL", "http://sglang:8000/v1")
    monkeypatch.setenv("SOCTALK_DEFAULT_FAST_TIER_MODEL", "Qwen/Qwen3-32B")
    monkeypatch.setenv("SOCTALK_DEFAULT_FAST_TIER_ENGINE", "sglang")
    monkeypatch.setenv("SOCTALK_DEFAULT_FAST_TIER_API_KEY", "sk-qwen")
    out = _install_default_llm_tiers()
    assert out["fast"]["model"] == "Qwen/Qwen3-32B"
    assert out["fast"]["engine"] == "sglang"
    assert out["fast"]["api_key_plain"] == "sk-qwen"
    # A partial config (no model) is ignored, not half-applied.
    monkeypatch.delenv("SOCTALK_DEFAULT_FAST_TIER_MODEL")
    assert _install_default_llm_tiers() is None


def test_validate_llm_tiers_ok_normalizes():
    out = validate_llm_tiers({"fast": _FAST})
    assert out["fast"]["provider"] == "openai-compatible"
    assert out["fast"]["engine"] == "sglang"
    # exclude_none: an omitted engine isn't stored as null.
    out2 = validate_llm_tiers({"reasoning": {"provider": "anthropic",
                                             "base_url": "https://api.anthropic.com",
                                             "model": "claude-sonnet-4-6"}})
    assert "engine" not in out2["reasoning"]


def test_validate_llm_tiers_none_and_empty():
    assert validate_llm_tiers(None) is None
    assert validate_llm_tiers({}) is None


def test_validate_llm_tiers_unknown_tier_rejected():
    with pytest.raises(ValueError, match="unknown llm_tiers"):
        validate_llm_tiers({"verdict": _FAST})  # not a real tier (fast/reasoning/chat/extraction)


def test_validate_llm_tiers_bad_block_rejected():
    with pytest.raises(ValueError):
        validate_llm_tiers({"fast": {"provider": "bedrock", "base_url": "x", "model": "m"}})
    with pytest.raises(ValueError):  # extra field (extra=forbid)
        validate_llm_tiers({"fast": {**_FAST, "surprise": 1}})


# -------------------------------------------------- decoding + combo matrix


def test_validate_llm_tiers_decoding_mode_stored():
    out = validate_llm_tiers({"fast": {**_FAST, "decoding_mode": "json_object"}})
    assert out["fast"]["decoding_mode"] == "json_object"


def test_validate_llm_tiers_anthropic_rejects_served_engine():
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        validate_llm_tiers({"reasoning": {"provider": "anthropic",
                                          "base_url": "https://api.anthropic.com",
                                          "model": "claude-opus-4", "engine": "vllm"}})


def test_validate_llm_tiers_anthropic_rejects_json_object():
    with pytest.raises(ValueError, match="not available on Anthropic"):
        validate_llm_tiers({"reasoning": {"provider": "anthropic",
                                          "base_url": "https://api.anthropic.com",
                                          "model": "claude-opus-4",
                                          "decoding_mode": "json_object"}})


def test_validate_llm_tiers_anthropic_allows_json_schema_strict():
    # Runtime maps json_schema_strict → tool_use on Anthropic (resolve_decoding_
    # mode), so the API must NOT 422 it — parity with the runtime resolver.
    out = validate_llm_tiers({"reasoning": {"provider": "anthropic",
                                            "base_url": "https://api.anthropic.com",
                                            "model": "claude-opus-4",
                                            "decoding_mode": "json_schema_strict"}})
    assert out["reasoning"]["decoding_mode"] == "json_schema_strict"


def test_validate_llm_tiers_guided_requires_served_engine():
    # Guided shaping is only implemented for vllm/sglang (guided_request_kwargs).
    for bad_engine in ("frontier", "openai_compatible", None):
        block = {**_FAST, "decoding_mode": "guided_json"}
        if bad_engine is None:
            block.pop("engine", None)
        else:
            block["engine"] = bad_engine
        with pytest.raises(ValueError, match="needs a served engine"):
            validate_llm_tiers({"fast": block})
    # vllm / sglang accept guided modes.
    ok = validate_llm_tiers({"fast": {**_FAST, "engine": "vllm",
                                      "decoding_mode": "guided_grammar"}})
    assert ok["fast"]["decoding_mode"] == "guided_grammar"


def test_validate_llm_tiers_bad_base_url_rejected():
    with pytest.raises(ValueError, match="http"):
        validate_llm_tiers({"fast": {**_FAST, "base_url": "sglang.internal:8000"}})


def test_validate_llm_tiers_error_never_leaks_key():
    # A bad block that also carries a secret must not echo the plaintext back.
    with pytest.raises(ValueError) as exc:
        validate_llm_tiers({"fast": {"provider": "anthropic",
                                     "base_url": "https://api.anthropic.com",
                                     "model": "m", "engine": "sglang",
                                     "api_key_plain": "sk-super-secret"}})
    assert "sk-super-secret" not in str(exc.value)


# ------------------------------------------------ key keep/replace/clear merge


def test_merge_tier_keys_keeps_absent():
    prior = {"fast": {"provider": "openai-compatible", "api_key_plain": "sk-old"}}
    # Incoming omits api_key_plain (sanitized round-trip) → carry it forward.
    merged = _merge_tier_keys(prior, {"fast": {"provider": "openai-compatible",
                                               "model": "new-model"}})
    assert merged["fast"]["api_key_plain"] == "sk-old"
    assert merged["fast"]["model"] == "new-model"


def test_merge_tier_keys_replaces_when_present():
    prior = {"fast": {"api_key_plain": "sk-old"}}
    merged = _merge_tier_keys(prior, {"fast": {"api_key_plain": "sk-new"}})
    assert merged["fast"]["api_key_plain"] == "sk-new"


def test_merge_tier_keys_clears_on_empty():
    prior = {"fast": {"api_key_plain": "sk-old"}}
    merged = _merge_tier_keys(prior, {"fast": {"provider": "anthropic",
                                               "api_key_plain": "  "}})
    assert "api_key_plain" not in merged["fast"]


def test_merge_tier_keys_no_prior():
    merged = _merge_tier_keys(None, {"fast": {"model": "m"}})
    assert "api_key_plain" not in merged["fast"]


def test_sanitize_tiers_strips_plaintext():
    sane = _sanitize_tiers({"fast": {"provider": "openai-compatible",
                                     "base_url": "http://x:8000/v1", "model": "m",
                                     "engine": "sglang", "decoding_mode": "json_object",
                                     "api_key_plain": "sk-secret"}})
    assert sane["fast"]["has_api_key"] is True
    assert sane["fast"]["decoding_mode"] == "json_object"
    assert "api_key_plain" not in sane["fast"]


# -------------------------------------------------------- byte-identical guard


def test_single_provider_render_has_no_tier_keys():
    v = _render(_integration(uuid4()))
    assert "tiers" not in v["llm"]
    assert "tierKeys" not in v["llm"]
    assert "extraLlmEgressPorts" not in v["networkPolicies"]


# --------------------------------------------------------------- tier render


def test_hybrid_render_tiers_and_ports():
    integ = _integration(uuid4(), llm_tiers=validate_llm_tiers({"fast": _FAST}))
    v = _render(integ)
    fast = v["llm"]["tiers"]["fast"]
    assert fast["provider"] == "openai"  # canonicalized from openai-compatible
    assert fast["baseUrl"] == "http://sglang.internal:8000/v1"
    assert fast["model"] == "qwen3-32b"
    assert fast["engine"] == "sglang"
    # Own key materialized into tenant-llm-key on the L2 (chart-owned) path.
    assert v["llm"]["tierKeys"]["fast"] == "sk-served"
    # sglang :8000 is distinct from the primary anthropic :443 → port union.
    assert v["networkPolicies"]["extraLlmEgressPorts"] == [8000]


def test_hybrid_render_emits_decoding_mode():
    integ = _integration(uuid4(), llm_tiers=validate_llm_tiers(
        {"fast": {**_FAST, "decoding_mode": "json_object"}}))
    v = _render(integ)
    # render maps snake_case decoding_mode → chart camelCase decodingMode.
    assert v["llm"]["tiers"]["fast"]["decodingMode"] == "json_object"


def test_validate_llm_tiers_per_tier_sampling():
    out = validate_llm_tiers({"fast": {**_FAST, "temperature": 0.3, "max_tokens": 2048}})
    assert out["fast"]["temperature"] == 0.3
    assert out["fast"]["max_tokens"] == 2048
    # Bounds mirror the global knobs.
    with pytest.raises(ValueError):
        validate_llm_tiers({"fast": {**_FAST, "temperature": 2.5}})
    with pytest.raises(ValueError):
        validate_llm_tiers({"fast": {**_FAST, "max_tokens": 8193}})


def test_validate_llm_tiers_rejects_bool_sampling():
    # Parity with the global sampling API — True must not coerce to 1.0 / 1.
    with pytest.raises(ValueError):
        validate_llm_tiers({"fast": {**_FAST, "temperature": True}})
    with pytest.raises(ValueError):
        validate_llm_tiers({"fast": {**_FAST, "max_tokens": True}})


def test_hybrid_render_emits_per_tier_sampling():
    integ = _integration(uuid4(), llm_tiers=validate_llm_tiers(
        {"fast": {**_FAST, "temperature": 0.0, "max_tokens": 2048}}))
    v = _render(integ)
    fast = v["llm"]["tiers"]["fast"]
    # temperature 0.0 must survive (presence, not truthiness).
    assert fast["temperature"] == 0.0
    assert fast["maxTokens"] == 2048
    # A tier without sampling emits neither key.
    plain = _render(_integration(uuid4(), llm_tiers=validate_llm_tiers({"fast": _FAST})))
    assert "temperature" not in plain["llm"]["tiers"]["fast"]
    assert "maxTokens" not in plain["llm"]["tiers"]["fast"]


def test_resolve_tier_sampling_override_and_fallback():
    from soctalk.inference import InferenceTier, resolve_tier_sampling

    class Cfg:
        tiers = {"router": {"temperature": 0.3, "max_tokens": 2048}, "reasoning": {}}

    # Router tier overrides the caller default.
    s = resolve_tier_sampling(Cfg(), InferenceTier.ROUTER, temperature=0.0, max_tokens=4096)
    assert (s.temperature, s.max_tokens) == (0.3, 2048)
    # Reasoning tier present but no sampling → caller defaults win.
    r = resolve_tier_sampling(Cfg(), InferenceTier.REASONING, temperature=0.1, max_tokens=2048)
    assert (r.temperature, r.max_tokens) == (0.1, 2048)
    # No tiers at all → defaults.
    n = resolve_tier_sampling(object(), InferenceTier.ROUTER, temperature=0.5, max_tokens=1024)
    assert (n.temperature, n.max_tokens) == (0.5, 1024)


def test_render_emits_run_budget_only_when_set():
    # Dollar budget still env-renders (issue #5). The per-run TOKEN budget is
    # DB-resolved at run creation now (#103) and is NOT rendered to worker env,
    # even when the legacy column is set.
    v = _render(_integration(uuid4(), llm_dollar_budget_per_run=2.5,
                             llm_token_budget_per_run=50000))
    assert v["llm"]["dollarBudgetPerRun"] == 2.5
    assert "tokenBudgetPerRun" not in v["llm"]
    plain = _render(_integration(uuid4()))
    assert "dollarBudgetPerRun" not in plain["llm"]
    assert "tokenBudgetPerRun" not in plain["llm"]


def test_render_emits_global_sampling():
    # Tenant-global sampling flows into values.llm → SOCTALK_LLM_* worker env.
    v = _render(_integration(uuid4(), llm_temperature=0.7, llm_max_tokens=512))
    assert v["llm"]["temperature"] == 0.7
    assert v["llm"]["maxTokens"] == 512
    # Defaults still emitted (columns carry defaults) so env reflects the tenant.
    d = _render(_integration(uuid4()))
    assert d["llm"]["temperature"] == 0.0
    assert d["llm"]["maxTokens"] == 4096


def test_render_secret_checksum_rolls_on_key_change():
    # A key-only tier rotation must change the rollout checksum so helm upgrade
    # rolls the worker (env-from-secret doesn't hot-reload) — Codex review #2.
    tid = uuid4()
    a = _render(_integration(tid, llm_tiers=validate_llm_tiers({"fast": _FAST})))
    rotated = {**_FAST, "api_key_plain": "sk-rotated"}
    b = _render(_integration(tid, llm_tiers=validate_llm_tiers({"fast": rotated})))
    assert a["llm"]["secretChecksum"] != b["llm"]["secretChecksum"]
    # Independent of include_llm_api_key — the L1 path withholds the plaintext
    # from values but the checksum still reflects the true material.
    c = _render(_integration(tid, llm_tiers=validate_llm_tiers({"fast": _FAST})),
                include_llm_api_key=False)
    assert c["llm"]["secretChecksum"] == a["llm"]["secretChecksum"]
    # Structural-only single-provider tenants still get a stable checksum.
    assert _render(_integration(tid))["llm"]["secretChecksum"]


def test_hybrid_render_l1_controller_path_omits_plaintext():
    integ = _integration(uuid4(), llm_tiers=validate_llm_tiers({"fast": _FAST}))
    v = _render(integ, include_llm_api_key=False)
    # Tier key present (so the worker env references it) but plaintext withheld
    # — the controller mirrors the real key into the Secret.
    assert v["llm"]["tierKeys"]["fast"] == ""


def test_llm_secret_data_includes_tier_own_keys():
    # The controller mirrors primary + per-tier own keys into one Secret.
    data = _llm_secret_data("ak-primary", {
        "fast": {"api_key_plain": "sk-served"},
        "reasoning": {},  # reuses primary, no own key
    })
    assert data == {"api_key": "ak-primary", "fast_api_key": "sk-served"}


def test_llm_secret_data_single_provider():
    assert _llm_secret_data("ak", None) == {"api_key": "ak"}


def test_cross_provider_tier_without_key_flagged():
    # anthropic primary + an openai fast tier with no own key can't authenticate.
    offending = _cross_provider_tiers_without_key(
        "anthropic", {"fast": {"provider": "openai-compatible"}})
    assert offending == ["fast"]
    # Same-provider tier (anthropic) reusing the primary key is fine.
    assert _cross_provider_tiers_without_key(
        "anthropic", {"reasoning": {"provider": "anthropic"}}) == []
    # Cross-provider tier WITH its own key is fine.
    assert _cross_provider_tiers_without_key(
        "anthropic", {"fast": {"provider": "openai", "api_key_plain": "sk"}}) == []
    # openai/openai-compatible collapse to one runtime provider.
    assert _cross_provider_tiers_without_key(
        "openai-compatible", {"fast": {"provider": "openai"}}) == []


def test_hybrid_same_port_no_extra_egress():
    # A tier whose backend shares the primary port (443) adds no extra port.
    integ = _integration(uuid4(), llm_tiers=validate_llm_tiers({
        "reasoning": {"provider": "anthropic", "base_url": "https://api.anthropic.com",
                      "model": "claude-opus-4"},
    }))
    v = _render(integ)
    assert "extraLlmEgressPorts" not in v["networkPolicies"]
    # No own key → not in tierKeys (reuses the primary credential).
    assert "tierKeys" not in v["llm"] or "reasoning" not in v["llm"].get("tierKeys", {})
