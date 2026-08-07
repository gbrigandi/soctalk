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


# -------------------------------------------------- Codex review regressions


def test_versioned_response_ids_still_hit_the_snapshot():
    """Providers answer with dated ids the configured model string lacks.

    Without stripping, a snapshot for ``deepseek-v4-flash`` is ignored the
    moment the API reports ``deepseek-v4-flash-20260731``, and the call falls
    back to the fail-expensive rate this feature exists to remove.
    """
    st = _state(
        {
            "version": 1,
            "models": {
                "fast": {
                    "model": "deepseek-v4-flash",
                    "source": "catalog",
                    "input_per_mtok": 0.206,
                    "output_per_mtok": 0.412,
                }
            },
        }
    )
    for reported in (
        "deepseek-v4-flash",
        "deepseek-v4-flash-20260731",
        "deepseek-v4-flash-latest",
    ):
        assert budget._snapshot_rates(st, reported) == {
            "input": 0.206,
            "output": 0.412,
        }, reported
    # A different SKU must not be folded in.
    assert budget._snapshot_rates(st, "deepseek-v4-pro") is None


def test_one_model_at_two_prices_falls_through_rather_than_guessing():
    """A hybrid tenant can run one model string through two providers.

    The call site does not say which role is spending, so picking either would
    bill half the calls at the other provider's rate. Falling through to the
    table is the honest answer.
    """
    ambiguous = _state(
        {
            "version": 1,
            "models": {
                "fast": {
                    "model": "m",
                    "source": "catalog",
                    "input_per_mtok": 0.1,
                    "output_per_mtok": 0.2,
                },
                "reasoning": {
                    "model": "m",
                    "source": "catalog",
                    "input_per_mtok": 9.0,
                    "output_per_mtok": 9.0,
                },
            },
        }
    )
    assert budget._snapshot_rates(ambiguous, "m") is None

    # Same model at the same price under both roles is not ambiguous at all.
    agreed = _state(
        {
            "version": 1,
            "models": {
                "fast": {
                    "model": "m",
                    "source": "catalog",
                    "input_per_mtok": 0.1,
                    "output_per_mtok": 0.2,
                },
                "reasoning": {
                    "model": "m",
                    "source": "catalog",
                    "input_per_mtok": 0.1,
                    "output_per_mtok": 0.2,
                },
            },
        }
    )
    assert budget._snapshot_rates(agreed, "m") == {"input": 0.1, "output": 0.2}


def test_seed_prices_file_is_importable_as_shipped():
    """The shipped seed must satisfy the importer's own validation.

    A seed that the CLI would reject is worse than no seed: the operator finds
    out at install time, on the one path that is supposed to just work.
    """
    import json
    from pathlib import Path

    from soctalk.core.cli.prices import _parse_entries

    seed = Path(__file__).resolve().parents[2] / "data" / "pricing" / "seed-prices.json"
    entries = _parse_entries(json.loads(seed.read_text()))
    assert len(entries) >= 10
    # Self-hosted entries are deliberately zero: the marginal token cost really
    # is nothing, and the GPU hour is accounted elsewhere.
    zero = [e for e in entries if e["provider_kind"] == "self_hosted"]
    assert zero and all(
        e["dimensions"]["input_per_mtok_microusd"] == 0 for e in zero
    )
    # Every entry carries an as_of, because a price with no date is a price
    # nobody can judge the staleness of.
    assert all(e["as_of"] is not None for e in entries)


def test_a_gateways_price_is_attributed_to_the_gateway_not_the_upstream():
    """A reseller's rate card is its own, and the resolver must reach it.

    Caught on a live deployment: the seed labelled a gateway-served
    ``deepseek-v4-flash`` with the upstream vendor's slug while carrying the
    gateway's price, and the resolver derives the slug from the host, so the
    entry was unreachable and every run resolved ``unknown``. The two prices
    differ by up to 2.3x, so attributing one to the other is not a cosmetic
    mistake.
    """
    import json
    from pathlib import Path

    assert provider_id_for("https://novarouteai.com/v1") == "novaroute"

    seed = Path(__file__).resolve().parents[2] / "data" / "pricing" / "seed-prices.json"
    entries = json.loads(seed.read_text())
    gateway_rows = [
        e for e in entries if "NovaRoute" in (e.get("notes") or "")
    ]
    assert gateway_rows, "expected gateway-priced rows in the seed"
    for row in gateway_rows:
        # Whoever bills is who the slug names.
        assert row["provider_id"] == "novaroute", row["model"]
