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


# ------------------------------------------------------ retired env overlay


def _snapshot(model, inp, out, source="catalog"):
    return {
        "version": 1,
        "models": {
            "fast": {
                "model": model,
                "source": source,
                "input_per_mtok": inp,
                "output_per_mtok": out,
            }
        },
    }


def _dollars_with(snapshot, model, inp=1_000_000, out=0):
    st = {}
    budget.ensure(st)
    st["price_snapshot"] = snapshot
    return budget._cost_dollars(
        inp, out, model, rates=budget._snapshot_rates(st, model)
    )


def test_env_overlay_is_retired(monkeypatch):
    """``SOCTALK_MODEL_PRICES`` no longer prices anything (#125).

    It was keyed by model string alone when the same model costs different
    amounts at different providers, it needed a helm upgrade and a pod restart
    to change, and it left no trace of which rate a run was billed at. The
    catalog and the per-run snapshot replace it on all three counts.
    """
    monkeypatch.setenv(
        "SOCTALK_MODEL_PRICES", '{"qwen3-32b": {"input": 0.2, "output": 0.6}}'
    )
    budget._price_cache = None
    # Ignored: the model is still unpriced, so the fallback applies.
    assert _dollars("qwen3-32b") == pytest.approx(15.0)
    # And a model the built-in table ships is unaffected by the env either way.
    assert _dollars("gpt-4o-mini") == pytest.approx(0.15)


def test_a_self_hosted_model_is_priced_by_its_snapshot():
    """What the overlay used to do, done by the mechanism that replaced it."""
    assert _dollars_with(
        _snapshot("qwen3-32b", 0.2, 0.6), "qwen3-32b", inp=1_000_000, out=1_000_000
    ) == pytest.approx(0.8)


def test_zero_cost_is_expressible_for_local_inference():
    """Self-hosted marginal token cost really is nothing, and must survive.

    A zero rate has to be distinguishable from a missing one, or local
    inference gets billed at the fail-expensive fallback.
    """
    assert _dollars_with(
        _snapshot("llama3-70b", 0.0, 0.0), "llama3-70b", inp=5_000_000, out=2_000_000
    ) == 0.0


def test_a_snapshot_overrides_a_stale_builtin_rate():
    assert _dollars_with(_snapshot("gpt-4o-mini", 1.0, 2.0), "gpt-4o-mini") == pytest.approx(1.0)


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


def test_the_live_mispricing_is_corrected_by_a_resolved_price():
    """The incident that motivated the feature, pinned (#121, #125).

    The call shape is verbatim from a run on a live install: 1,316 input tokens
    (1,024 of them cache reads) and 2,252 output tokens on a gateway-served
    ``deepseek-v4-flash``. Unpriced it recorded $0.174816 at the fail-expensive
    fallback; at the model's real rate it is a tenth of a cent, and that gap is
    what halts runs on spend that never happened.
    """
    unpriced = budget._cost_dollars(1316, 2252, "deepseek-v4-flash", cache_read_tokens=1024)
    assert unpriced == pytest.approx(0.174816, abs=1e-6)

    st = {}
    budget.ensure(st)
    st["price_snapshot"] = _snapshot("deepseek-v4-flash", 0.206, 0.412)
    priced = budget._cost_dollars(
        1316, 2252, "deepseek-v4-flash",
        cache_read_tokens=1024,
        rates=budget._snapshot_rates(st, "deepseek-v4-flash"),
    )
    # (292 uncached + 1024 cache-read at 10%) input + 2252 output, at 0.206/0.412.
    assert priced == pytest.approx(0.001009, abs=1e-6)
    assert unpriced > priced * 100
