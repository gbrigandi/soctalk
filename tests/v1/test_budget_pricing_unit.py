"""Configurable model pricing for the budget guard (issue #5).

The dollar cap priced any model absent from the built-in table at the Opus
fallback rate ($15/$75 per Mtok) — silently halting self-hosted / newly
released models on phantom spend. These cover the overlay, the opt-in
fallback, and the visible-warning behavior that fix adds.
"""

from __future__ import annotations

import pytest

from soctalk.graph import budget


@pytest.fixture(autouse=True)
def _reset_pricing_state(monkeypatch):
    # Clear the env-keyed overlay cache and warn-once set so each test starts
    # from the built-in defaults regardless of order.
    monkeypatch.delenv("SOCTALK_MODEL_PRICES", raising=False)
    monkeypatch.delenv("SOCTALK_UNKNOWN_MODEL_COST", raising=False)
    budget._price_cache = None
    budget._warned_unpriced.clear()
    yield
    budget._price_cache = None
    budget._warned_unpriced.clear()


def _dollars(model, inp=1_000_000, out=0):
    return budget._cost_dollars(inp, out, model)


# ------------------------------------------------------------------ baselines


def test_known_model_prices_from_builtin_table():
    # 1M input tokens of gpt-4o-mini at $0.15/Mtok.
    assert _dollars("gpt-4o-mini") == pytest.approx(0.15)


def test_unknown_model_defaults_to_fail_expensive_opus_rate():
    # Unchanged default behaviour: unpriced model -> Opus $15/Mtok input.
    assert _dollars("qwen3-32b") == pytest.approx(15.0)


def test_unknown_model_warns_once(monkeypatch):
    warnings: list[tuple] = []
    monkeypatch.setattr(budget.logger, "warning",
                        lambda ev, **kw: warnings.append((ev, kw)))
    _dollars("qwen3-32b")
    _dollars("qwen3-32b")  # second call must not re-warn
    _dollars("mistral-large")  # a different unpriced model warns separately
    events = [e for e, _ in warnings if e == "budget_unpriced_model_fallback"]
    assert len(events) == 2
    assert {w[1]["model"] for w in warnings if w[0] == "budget_unpriced_model_fallback"} \
        == {"qwen3-32b", "mistral-large"}


# ------------------------------------------------------------------- overlay


def test_overlay_adds_self_hosted_model(monkeypatch):
    monkeypatch.setenv("SOCTALK_MODEL_PRICES",
                       '{"qwen3-32b": {"input": 0.2, "output": 0.6}}')
    assert _dollars("qwen3-32b", inp=1_000_000, out=1_000_000) == pytest.approx(0.8)


def test_overlay_zero_cost_entry_for_local_inference(monkeypatch):
    monkeypatch.setenv("SOCTALK_MODEL_PRICES",
                       '{"llama3-70b": {"input": 0, "output": 0}}')
    assert _dollars("llama3-70b", inp=5_000_000, out=2_000_000) == 0.0


def test_overlay_corrects_builtin_rate(monkeypatch):
    monkeypatch.setenv("SOCTALK_MODEL_PRICES",
                       '{"gpt-4o-mini": {"input": 1.0, "output": 2.0}}')
    assert _dollars("gpt-4o-mini") == pytest.approx(1.0)


def test_overlay_matches_versioned_ids_via_normalization(monkeypatch):
    monkeypatch.setenv("SOCTALK_MODEL_PRICES",
                       '{"qwen3-32b": {"input": 0.5, "output": 1.0}}')
    # A served endpoint reporting a dated variant still hits the overlay key.
    assert _dollars("qwen3-32b-20250101") == pytest.approx(0.5)


def test_malformed_overlay_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("SOCTALK_MODEL_PRICES", "{not valid json")
    # Falls back to the built-in table without raising.
    assert _dollars("gpt-4o-mini") == pytest.approx(0.15)


# --------------------------------------------------- configurable fallback


def test_unknown_cost_zero_makes_unpriced_free(monkeypatch):
    monkeypatch.setenv("SOCTALK_UNKNOWN_MODEL_COST", "zero")
    assert _dollars("some-local-model", inp=9_000_000, out=9_000_000) == 0.0


def test_unknown_cost_zero_does_not_warn(monkeypatch):
    monkeypatch.setenv("SOCTALK_UNKNOWN_MODEL_COST", "0")
    warnings: list = []
    monkeypatch.setattr(budget.logger, "warning",
                        lambda ev, **kw: warnings.append(ev))
    _dollars("some-local-model")
    assert "budget_unpriced_model_fallback" not in warnings


def test_unknown_cost_custom_json_fallback(monkeypatch):
    monkeypatch.setenv("SOCTALK_UNKNOWN_MODEL_COST", '{"input": 2.0, "output": 4.0}')
    assert _dollars("mystery", inp=1_000_000, out=1_000_000) == pytest.approx(6.0)


def test_unknown_cost_malformed_falls_back_to_opus(monkeypatch):
    monkeypatch.setenv("SOCTALK_UNKNOWN_MODEL_COST", "banana")
    assert _dollars("mystery") == pytest.approx(15.0)


def test_overlay_prices_a_model_absent_from_the_builtin_table(monkeypatch):
    """A tenant-supplied price is what the model is billed at (#121).

    The numbers are the exact call shape from a live run that mispriced: 1,316
    input tokens (1,024 of them cache reads) and 2,252 output tokens on a
    gateway-served ``deepseek-v4-flash``. Unpriced it recorded $0.174816 at the
    $15/$75 fallback; at the model's real list price it is a tenth of a cent,
    and that gap is what halts runs on spend that never happened.
    """
    unpriced = budget._cost_dollars(1316, 2252, "deepseek-v4-flash", cache_read_tokens=1024)
    assert unpriced == pytest.approx(0.174816, abs=1e-6)

    monkeypatch.setenv(
        "SOCTALK_MODEL_PRICES",
        '{"deepseek-v4-flash": {"input": 0.206, "output": 0.412}}',
    )
    budget._price_cache = None
    priced = budget._cost_dollars(1316, 2252, "deepseek-v4-flash", cache_read_tokens=1024)

    # (292 uncached + 1024 cache-read at 10%) input + 2252 output, at 0.206/0.412.
    assert priced == pytest.approx(0.001009, abs=1e-6)
    assert unpriced > priced * 100
