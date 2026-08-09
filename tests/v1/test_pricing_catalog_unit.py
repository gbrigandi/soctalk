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

    # Stamped, and the model was unknown at resolve time: the sentinel, not
    # None. The run is billed by the story it recorded rather than being
    # quietly rescued by the shipped defaults.
    unknown = {
        "version": 1,
        "models": {"fast": {"model": "x-1", "source": "unknown"}},
    }
    assert budget._snapshot_rates(_state(unknown), "x-1") is budget._UNKNOWN_SENTINEL

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


# ------------------------------------------------- canonical usage + actuals


class _Resp:
    """Minimal stand-in for a LangChain response carrying provider metadata."""

    def __init__(self, usage, provider=None):
        self.usage_metadata = None
        self.response_metadata = {"usage": usage}
        if provider:
            self.response_metadata["provider"] = provider


def test_openai_style_cache_and_reasoning_details_are_read():
    """The shapes this codebase used to discard, taken from live responses.

    Reading only prompt_tokens/completion_tokens bills every cached token at
    the full input rate, which overstates spend on exactly the calls caching
    exists to make cheap, and loses the reasoning split entirely.
    """
    from soctalk.core.pricing.usage import canonical_usage

    u = canonical_usage(
        _Resp(
            {
                "prompt_tokens": 6,
                "completion_tokens": 17,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"reasoning_tokens": 16},
            }
        )
    )
    assert (u.input_tokens, u.output_tokens) == (6, 17)
    assert u.cache_read_tokens == 4
    assert u.reasoning_tokens == 16
    assert u.actual_cost_usd is None

    # Anthropic / LangChain shape reaches the same canonical fields.
    a = canonical_usage(
        _Resp(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "input_token_details": {"cache_read": 40, "cache_creation": 10},
            }
        )
    )
    assert (a.cache_read_tokens, a.cache_write_tokens) == (40, 10)


def test_a_provider_reported_cost_is_read_and_preferred():
    """OpenRouter returns the actual charge inline; no estimate beats it."""
    from soctalk.core.pricing.usage import canonical_usage

    u = canonical_usage(
        _Resp(
            {
                "prompt_tokens": 6,
                "completion_tokens": 9,
                "cost": 2.2512e-06,
                "completion_tokens_details": {"reasoning_tokens": 8},
            },
            provider="DeepSeek",
        )
    )
    assert u.actual_cost_usd == pytest.approx(2.2512e-06)
    # Gateways disclose which upstream really served the call, which is the
    # provider half of the (provider, model) key we otherwise infer.
    assert u.reported_provider == "DeepSeek"


def test_detail_counts_can_never_exceed_their_totals():
    """A provider over-reporting a subset must not invent billable tokens."""
    from soctalk.core.pricing.usage import canonical_usage

    u = canonical_usage(
        _Resp(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 999},
                "completion_tokens_details": {"reasoning_tokens": 999},
            }
        )
    )
    assert u.cache_read_tokens == 10
    assert u.reasoning_tokens == 5


def test_reasoning_is_billed_as_output_unless_a_rate_says_otherwise():
    """OpenAI, Anthropic, Gemini and DeepSeek all bill thinking as output.

    So the split must cost nothing by default, and only bite when a price
    names a separate reasoning rate, as OpenRouter's pricing object can.
    """
    plain = budget._cost_dollars(
        0, 1_000_000, "m", rates={"input": 1.0, "output": 10.0}
    )
    with_reasoning = budget._cost_dollars(
        0, 1_000_000, "m", reasoning_tokens=400_000, rates={"input": 1.0, "output": 10.0}
    )
    assert plain == pytest.approx(with_reasoning)

    separately_priced = budget._cost_dollars(
        0, 1_000_000, "m",
        reasoning_tokens=400_000,
        rates={"input": 1.0, "output": 10.0, "reasoning": 20.0},
    )
    # 600k at $10 + 400k at $20.
    assert separately_priced == pytest.approx(6.0 + 8.0)


def test_an_unknown_stamp_is_not_rescued_by_the_shipped_defaults():
    """A run must be billed by the story it recorded (Codex).

    ``gpt-4o`` is in the shipped defaults, so before this the run would record
    "unknown" and quietly be priced at the default anyway — one story on the
    row, another in the ledger.
    """
    st = _state({"version": 1, "models": {"fast": {"model": "gpt-4o", "source": "unknown"}}})
    rates = budget._snapshot_rates(st, "gpt-4o")
    assert rates is budget._UNKNOWN_SENTINEL
    # Priced by the unknown-model policy, not the $2.50 shipped default.
    assert budget._cost_dollars(1_000_000, 0, "gpt-4o", rates=rates) == pytest.approx(15.0)
    # A run with no snapshot at all is a different case and keeps the default.
    assert budget._cost_dollars(1_000_000, 0, "gpt-4o") == pytest.approx(2.5)


def test_seed_carries_every_model_the_builtin_table_priced():
    """Retiring the table cannot lose a price (Codex ordering: seed, then cut).

    Making unknown authoritative before the catalog knew these models would
    have sent every frontier model to the fail-expensive fallback.
    """
    import json
    from pathlib import Path

    from soctalk.graph.budget import _MODEL_PRICES_PER_MTOK as TABLE

    seed = Path(__file__).resolve().parents[2] / "data" / "pricing" / "seed-prices.json"
    seeded = {e["model"] for e in json.loads(seed.read_text())}
    missing = sorted(set(TABLE) - seeded)
    assert not missing, f"built-in models absent from the seed: {missing}"


# --- provider-reported cost: asking for it, and reading it back (#125) -------


def test_usage_accounting_requested_only_from_openrouter():
    """OpenRouter reports what it charged, but only when the request opts in.

    Sent everywhere it would break the reference implementation: ``usage`` is
    not an OpenAI request parameter and api.openai.com rejects unmodelled body
    fields, so the gate is on the host.
    """
    from soctalk.llm import _usage_accounting_kwargs

    assert _usage_accounting_kwargs("https://openrouter.ai/api/v1") == {
        "extra_body": {"usage": {"include": True}}
    }
    assert _usage_accounting_kwargs("https://gateway.openrouter.ai/v1") == {
        "extra_body": {"usage": {"include": True}}
    }
    for other in (
        "https://api.openai.com/v1",
        "https://novarouteai.com/v1",
        "https://api.anthropic.com",
        None,
    ):
        assert _usage_accounting_kwargs(other) == {}


def test_openrouter_cost_and_upstream_are_read_from_a_raw_response():
    """The live OpenRouter shape, reduced to the fields pricing depends on."""
    from soctalk.core.pricing.usage import canonical_usage

    usage = canonical_usage(
        {
            "provider": "DeepSeek",
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 33,
                "completion_tokens_details": {"reasoning_tokens": 32},
                "prompt_tokens_details": {"cached_tokens": 0},
                "cost": 1.162e-05,
            },
        }
    )
    assert usage.actual_cost_usd == 1.162e-05
    # The upstream is disclosed beside choices, not inside usage.
    assert usage.reported_provider == "DeepSeek"
    assert usage.reasoning_tokens == 32


def test_provider_reported_cost_beats_our_estimate():
    """An actual is authoritative: no rate card is consulted, and the run says so."""
    from soctalk.graph import budget

    state: dict = {}
    budget.ensure(state)
    budget.track(
        state,
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 1000,
                "cost": 0.000123,
            }
        },
    )
    assert state["dollars_used"] == 0.000123
    assert state["cost_basis"] == "provider_reported"


# ------------------------------------------------------- model-id normalization
#
# #139: the catalog lookup matched the raw model ID exactly, so a dated ID like
# ``claude-haiku-4-5-20251001`` — the form providers return, and the form
# operators are told to pin — missed the seeded ``claude-haiku-4-5`` row,
# resolved ``unknown``, and billed at the $15/$75 fail-expensive fallback.
# Measured at ~13x on a real run before the fix.


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-3-5-sonnet-20241022", "claude-3-5-sonnet"),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet"),
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        # Already a base ID: idempotent.
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("", ""),
    ],
)
def test_base_model_id_strips_only_version_suffixes(model, expected):
    from soctalk.core.pricing.names import base_model_id

    assert base_model_id(model) == expected


@pytest.mark.parametrize("model", ["gpt-4-32k", "gpt-4-vision", "gpt-4-turbo"])
def test_base_model_id_never_folds_a_distinct_sku(model):
    """A wrong strip under-charges silently; a missed strip fails loudly."""
    from soctalk.core.pricing.names import base_model_id

    assert base_model_id(model) == model


def test_the_model_demo_actually_runs_is_priced_not_unknown():
    """Regression for #139, pinned to the real deployment's model ID.

    demo.soctalk.ai runs ``claude-haiku-4-5-20251001``. Before the fix this
    resolved to the fail-expensive fallback through BOTH paths: the catalog
    matched exactly and missed, and the legacy table normalized correctly but
    had no ``claude-haiku-4-5`` row to land on.
    """
    normalized = budget._normalize_model("claude-haiku-4-5-20251001")
    assert normalized == "claude-haiku-4-5"
    assert normalized in budget._MODEL_PRICES_PER_MTOK

    rates = budget._MODEL_PRICES_PER_MTOK[normalized]
    fallback, _ = budget._unknown_model_cost()
    assert rates != fallback, "priced model must not resolve to the fallback rate"
    assert rates == {"input": 1.0, "output": 5.0}


def test_seeded_families_cover_the_shipped_model_defaults():
    """Normalizing is not enough if the table lacks the current families.

    This is the half of #139 that the regex fix alone would have missed.
    """
    for family in ("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-1"):
        assert family in budget._MODEL_PRICES_PER_MTOK, f"{family} unpriced"


# --- phase 3: overrides must be able to name a backend --------------------


def test_override_key_shape():
    from soctalk.core.pricing.resolve import override_key

    assert override_key("openai_compatible", "novaroute", "m") == (
        "openai_compatible:novaroute:m"
    )
    # No vendor behind the gateway: wildcard, so the override covers the
    # protocol rather than pretending to know the vendor.
    assert override_key("openai_compatible", None, "m") == "openai_compatible:*:m"


def test_qualified_override_beats_the_bare_one():
    """The same model string can cost two different amounts.

    A tenant pointing fast and reasoning at one model through two providers
    genuinely has two prices; the model-keyed map collapsed them into one.
    """
    from soctalk.core.pricing.resolve import _rates_from_override

    overrides = {
        "m": {"input": 1.0, "output": 2.0},
        "openai_compatible:novaroute:m": {"input": 10.0, "output": 20.0},
    }
    qualified = _rates_from_override(overrides, "m", "openai_compatible", "novaroute")
    assert qualified == {"input_per_mtok": 10.0, "output_per_mtok": 20.0}

    # A different backend falls through to the bare entry.
    other = _rates_from_override(overrides, "m", "anthropic", None)
    assert other == {"input_per_mtok": 1.0, "output_per_mtok": 2.0}


def test_wildcard_override_covers_a_protocol():
    from soctalk.core.pricing.resolve import _rates_from_override

    overrides = {"openai_compatible:*:m": {"input": 5.0, "output": 6.0}}
    got = _rates_from_override(overrides, "m", "openai_compatible", "some-gateway")
    assert got == {"input_per_mtok": 5.0, "output_per_mtok": 6.0}


def test_bare_overrides_keep_working():
    """The original shape is still honoured; this is additive."""
    from soctalk.core.pricing.resolve import _rates_from_override

    assert _rates_from_override({"m": {"input": 1.0, "output": 2.0}}, "m") == {
        "input_per_mtok": 1.0,
        "output_per_mtok": 2.0,
    }


def test_namespaced_models_are_matched_regardless_of_provider_id():
    """OpenRouter rows were unreachable, despite shipping in the seed.

    They are seeded with provider_id set to the UPSTREAM vendor ("deepseek"),
    while provider_id_for() returns "openrouter" from the host — so neither the
    exact nor the NULL lookup could reach them. A namespaced model id carries
    the vendor itself, which makes ignoring provider_id safe for that shape
    only (Codex phase-3 round 2).
    """
    import json
    import pathlib

    doc = json.loads(pathlib.Path("data/pricing/seed-prices.json").read_text())
    rows = doc if isinstance(doc, list) else (doc.get("models") or doc.get("prices"))
    openrouter = [r for r in rows if r.get("provider_kind") == "openrouter"]
    assert openrouter, "seed carries no openrouter rows"

    for r in openrouter:
        # The property the fallback relies on: the model names itself.
        assert "/" in r["model"], r["model"]
        # And the recorded provider_id is NOT what provider_id_for() returns
        # for the openrouter host — which is exactly why the fallback exists.
        assert r.get("provider_id") not in (None, "openrouter"), r


def test_form_kind_mapping_matches_the_backend():
    """The UI must derive provider_kind the way the resolver does.

    The form writes `<kind>:*:<model>` overrides, and the gate looks them up
    under the kind IT derives — which reads the host, not just the provider
    string. Mapping every non-Anthropic provider to openai_compatible wrote
    overrides that could never be found (Codex review of phases 4-5, round 2).
    """
    from soctalk.core.pricing.resolve import provider_kind_for

    def form_kind(provider: str, base_url: str) -> str:
        host = base_url.split("//")[-1].split("/")[0].lower() if base_url else ""
        if provider == "anthropic":
            return "anthropic"
        if host.endswith("openrouter.ai"):
            return "openrouter"
        if host.endswith("api.openai.com"):
            return "openai"
        return "openai_compatible"

    cases = [
        ("anthropic", ""),
        ("openai-compatible", "https://api.openai.com/v1"),
        ("openai-compatible", "https://openrouter.ai/api/v1"),
        ("openai-compatible", "https://novarouteai.com/v1"),
        ("openai-compatible", ""),
    ]
    for provider, url in cases:
        assert form_kind(provider, url) == provider_kind_for(provider, url or None), (
            provider,
            url,
        )


def test_anthropic_host_is_recognised_like_openais():
    """Found on a live install, not by reading the code.

    A tenant pointed at api.anthropic.com with provider left as
    "openai-compatible" classified as openai_compatible and missed every
    anthropic catalog row, so a seeded model (claude-sonnet-4-6) read as
    unpriced. The host says which vendor is billing, and api.openai.com was
    already treated that way.
    """
    from soctalk.core.pricing.resolve import provider_kind_for

    assert provider_kind_for("openai-compatible", "https://api.anthropic.com") == "anthropic"
    assert provider_kind_for("openai-compatible", "https://api.openai.com/v1") == "openai"
    # A genuine gateway is unaffected.
    assert (
        provider_kind_for("openai-compatible", "https://novarouteai.com/v1")
        == "openai_compatible"
    )
