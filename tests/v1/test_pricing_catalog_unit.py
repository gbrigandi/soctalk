"""The price catalog and the per-run snapshot it feeds (#125).

Unit-level: no database. The catalog lookup and resolver are exercised against
Postgres in the integration suite; what is pinned here is the arithmetic and
the fallback behaviour, which is where an error would silently mis-bill a
tenant rather than fail loudly.
"""

from __future__ import annotations

import pytest

from soctalk.core.pricing import catalog
from soctalk.core.pricing.resolve import provider_id_for, provider_kind_for
from soctalk.graph import budget


# ----------------------------------------------------------------- catalog


def test_dimensions_round_trip_through_micro_dollars():
    dims = catalog.dimensions_from_dollars(0.206, 0.412)
    # Integers, because float dollars do not survive arithmetic honestly.
    assert dims == {
        "input_per_mtok_microusd": 206_000,
        "output_per_mtok_microusd": 412_000,
    }
    assert catalog.dollars_per_mtok(dims, "input_per_mtok_microusd") == 0.206
    assert catalog.dollars_per_mtok(dims, "cache_read_per_mtok_microusd") is None


def test_validate_dimensions_keeps_unknown_axes_but_rejects_bad_known_ones():
    # A vendor inventing an axis should not need a schema change to be recorded.
    ok = catalog.validate_dimensions(
        {
            "input_per_mtok_microusd": 206_000,
            "output_per_mtok_microusd": 412_000,
            "some_future_axis_per_mtok_microusd": 5,
        }
    )
    assert ok["some_future_axis_per_mtok_microusd"] == 5

    for bad in (
        {"input_per_mtok_microusd": 1},                                   # missing output
        {"input_per_mtok_microusd": -1, "output_per_mtok_microusd": 2},   # negative
        {"input_per_mtok_microusd": 1.5, "output_per_mtok_microusd": 2},  # not whole micros
        {"input_per_mtok_microusd": True, "output_per_mtok_microusd": 2}, # bool is not a price
        "not-an-object",
    ):
        with pytest.raises(ValueError):
            catalog.validate_dimensions(bad)


# --------------------------------------------------------------- provider


def test_provider_kind_reads_the_host_not_just_the_provider_string():
    # "openai-compatible" is a protocol, not a provider: it identifies nothing
    # on its own, so the host is what distinguishes a gateway from an origin.
    assert provider_kind_for("anthropic", "https://api.anthropic.com") == "anthropic"
    assert provider_kind_for("openai", "https://api.openai.com/v1") == "openai"
    assert provider_kind_for("openai", "https://openrouter.ai/api/v1") == "openrouter"
    assert (
        provider_kind_for("openai-compatible", "https://gateway.example/v1")
        == "openai_compatible"
    )
    # Vendor attributed only where the host actually says so; a guess that is
    # wrong would price against another vendor's rate card.
    assert provider_id_for("https://api.deepseek.com/v1") == "deepseek"
    assert provider_id_for("https://gateway.example/v1") is None


# ---------------------------------------------------------------- pricing


def _state(snapshot=None):
    st = {}
    budget.ensure(st)
    if snapshot is not None:
        st["price_snapshot"] = snapshot
    return st


SNAPSHOT = {
    "version": 1,
    "currency": "USD",
    "models": {
        "fast": {
            "model": "deepseek-v4-flash",
            "source": "catalog",
            "input_per_mtok": 0.206,
            "output_per_mtok": 0.412,
        },
        "reasoning": {
            "model": "claude-sonnet-4-6",
            "source": "tenant_override",
            "input_per_mtok": 3.0,
            "output_per_mtok": 15.0,
        },
    },
}


def test_run_snapshot_prices_each_role_on_its_own_rates():
    """The two-model problem: one run, two models, two different prices."""
    st = _state(SNAPSHOT)

    fast = budget._cost_dollars(
        1316, 2252, "deepseek-v4-flash",
        cache_read_tokens=1024,
        rates=budget._snapshot_rates(st, "deepseek-v4-flash"),
    )
    # The exact call shape from the live run that mispriced at $0.174816.
    assert fast == pytest.approx(0.001009, abs=1e-6)

    reasoning = budget._cost_dollars(
        1_000_000, 1_000_000, "claude-sonnet-4-6",
        rates=budget._snapshot_rates(st, "claude-sonnet-4-6"),
    )
    assert reasoning == pytest.approx(18.0)


def test_missing_or_unknown_snapshot_falls_through_to_the_legacy_path():
    """A run created before this existed must price exactly as it did before."""
    unstamped = budget._cost_dollars(1316, 2252, "deepseek-v4-flash", cache_read_tokens=1024)
    assert unstamped == pytest.approx(0.174816, abs=1e-6)

    # No snapshot at all.
    assert budget._snapshot_rates(_state(), "deepseek-v4-flash") is None

    # Stamped, but the model was unknown at resolve time: must fall through to
    # the table rather than price at zero, which would hide the gap entirely.
    unknown = {
        "version": 1,
        "models": {"fast": {"model": "x-1", "source": "unknown"}},
    }
    assert budget._snapshot_rates(_state(unknown), "x-1") is None

    # A model the snapshot does not mention at all.
    assert budget._snapshot_rates(_state(SNAPSHOT), "some-other-model") is None

    # Malformed entry is treated as absent, never as free.
    broken = {
        "version": 1,
        "models": {"fast": {"model": "x-2", "source": "catalog", "input_per_mtok": "n/a"}},
    }
    assert budget._snapshot_rates(_state(broken), "x-2") is None


def test_snapshot_may_carry_explicit_cache_rates():
    """Cache axes default to the Anthropic-shaped derivation unless stated."""
    derived = budget._cost_dollars(
        2000, 0, "m",
        cache_read_tokens=1000,
        rates={"input": 1.0, "output": 2.0},
    )
    # 1000 uncached at $1/Mtok + 1000 cache reads at 10% of that.
    assert derived == pytest.approx(0.0011)

    explicit = budget._cost_dollars(
        2000, 0, "m",
        cache_read_tokens=1000,
        rates={"input": 1.0, "output": 2.0, "cache_read": 0.5},
    )
    assert explicit == pytest.approx(0.0015)
