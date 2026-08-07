"""Per-case_run LLM cost budget — tokens and dollars.

Two caps are enforced per case_run:

* ``tokens_budget`` (default ``SOCTALK_CASE_RUN_TOKEN_BUDGET`` or 15000)
* ``dollars_budget`` (default ``SOCTALK_CASE_RUN_DOLLAR_BUDGET`` or 5.0)

The supervisor calls ``ensure`` before its loop body, then short-circuits
to ``CLOSE`` when ``over_budget`` returns True. Nodes that call into an
LLM call ``track`` after every ``ainvoke`` so accumulation happens at the
same place the cost is incurred.

The dollar cap is the load-bearing guardrail: token counts are a noisy
proxy for spend (input and output tokens differ in price by 5x for
Sonnet, 5x for Opus, etc., and Opus is ~10x Sonnet). The historical
``tokens_used`` field is preserved so existing dashboards and the
``halted_budget`` disposition keep working unchanged.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog

logger = structlog.get_logger()


_DEFAULT_TOKEN_BUDGET = 15_000
_DEFAULT_DOLLAR_BUDGET = 5.0


# Approximate per-million-token prices for the models SocTalk supports.
# Kept conservative (round up where vendor pricing has tiers) so the cap
# fails closed rather than open. The cap doesn't need to be exact — it
# is a safety net, not a billing source of truth.
#
# Last reviewed: 2026-05.
#
# Keys are normalized model-family prefixes (see ``_normalize_model``).
# Both Anthropic and OpenAI return versioned model IDs — e.g.
# ``claude-3-5-sonnet-latest``, ``gpt-4o-2024-08-06`` — and we strip
# the version suffix before lookup. Without that strip every versioned
# response misses the table and gets billed at the Opus fallback rate,
# halting runs many times earlier than the configured dollar cap.
_MODEL_PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    # Anthropic Claude — public list price, $/MTok
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-opus-3": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-7-sonnet": {"input": 3.0, "output": 15.0},
    "claude-haiku-4": {"input": 1.0, "output": 5.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    # OpenAI — public list price, $/MTok
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "gpt-4": {"input": 30.0, "output": 60.0},
    "o1-mini": {"input": 3.0, "output": 12.0},
    "o1": {"input": 15.0, "output": 60.0},
}

# Fall-back price applied when the model name isn't in the table. Picked
# to be on the high side — an unknown model is more likely to be a
# premium tier than a free one, and we prefer to halt early. This
# fail-expensive default is correct for hosted APIs but actively wrong
# for a self-hosted endpoint (marginal cost ~0), which is why the
# fallback is overridable per deployment (SOCTALK_UNKNOWN_MODEL_COST).
_UNKNOWN_MODEL_FALLBACK = {"input": 15.0, "output": 75.0}
_ZERO_COST = {"input": 0.0, "output": 0.0}


def _parse_price_map(raw: str | None) -> dict[str, dict[str, float]]:
    """Parse a ``{"model-prefix": {"input": x, "output": y}}`` JSON map.

    Keys are normalized model-family prefixes (same shape as the built-in
    table); malformed entries are skipped, not fatal.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("budget_price_override_parse_failed")
        return {}
    out: dict[str, dict[str, float]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and "input" in v and "output" in v:
                try:
                    out[str(k)] = {"input": float(v["input"]), "output": float(v["output"])}
                except (ValueError, TypeError):
                    continue
    return out


# Cache the parsed overlay keyed on the raw env string so ``track`` doesn't
# re-parse JSON every call, while still reflecting env changes (tests, reloads).
_price_cache: tuple[str | None, dict[str, dict[str, float]]] | None = None


def _effective_prices() -> dict[str, dict[str, float]]:
    """The built-in price table overlaid with ``SOCTALK_MODEL_PRICES``.

    An overlay entry adds a self-hosted / newly-released model (or a
    ``{"input": 0, "output": 0}`` zero-cost entry for local inference) or
    corrects a stale built-in rate — without editing code.
    """
    global _price_cache
    raw = os.getenv("SOCTALK_MODEL_PRICES")
    if _price_cache is not None and _price_cache[0] == raw:
        return _price_cache[1]
    overrides = _parse_price_map(raw)
    merged = _MODEL_PRICES_PER_MTOK if not overrides else {**_MODEL_PRICES_PER_MTOK, **overrides}
    _price_cache = (raw, merged)
    return merged


def _unknown_model_cost() -> tuple[dict[str, float], bool]:
    """Resolve the fallback price for an unpriced model.

    Returns ``(price, explicit)`` — ``explicit`` is True when the deployment
    configured ``SOCTALK_UNKNOWN_MODEL_COST`` (``zero``/``free``/``0`` for
    local-only, or a ``{"input": x, "output": y}`` JSON), so callers can stay
    quiet about an intentional choice and only warn on the fail-expensive
    default.
    """
    raw = (os.getenv("SOCTALK_UNKNOWN_MODEL_COST") or "").strip()
    if not raw:
        return _UNKNOWN_MODEL_FALLBACK, False
    if raw.lower() in ("0", "zero", "free"):
        return _ZERO_COST, True
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "input" in data and "output" in data:
            return {"input": float(data["input"]), "output": float(data["output"])}, True
    except (ValueError, TypeError):
        pass
    logger.warning("budget_unknown_cost_parse_failed", value=raw)
    return _UNKNOWN_MODEL_FALLBACK, False


# Warn once per unpriced model so the fail-expensive fallback is visible
# instead of silently halting runs, without spamming the log every call.
_warned_unpriced: set[str] = set()


def _warn_unpriced_once(model: str | None, price: dict[str, float]) -> None:
    key = model or "<none>"
    if key in _warned_unpriced:
        return
    _warned_unpriced.add(key)
    logger.warning(
        "budget_unpriced_model_fallback",
        model=model,
        input_price_per_mtok=price["input"],
        output_price_per_mtok=price["output"],
        hint="add it to SOCTALK_MODEL_PRICES, or set SOCTALK_UNKNOWN_MODEL_COST=zero for local inference",
    )


_VERSION_SUFFIX_RE = re.compile(
    # Trailing ``-YYYYMMDD`` (Anthropic style), ``-YYYY-MM-DD`` (OpenAI
    # style), or the literal ``-latest`` alias. We strip ONLY these
    # known suffix shapes — never a free-form trailing token — because
    # variants like ``gpt-4-32k`` or ``gpt-4-vision`` are *different
    # SKUs* with different pricing and must not be folded into the
    # base family.
    r"(?:-(?:\d{8}|\d{4}-\d{2}-\d{2})|-latest)$"
)


def _normalize_model(model: str | None, prices: dict[str, dict[str, float]] | None = None) -> str:
    """Strip date / latest suffixes so versioned model IDs hit the table.

    Examples:
      ``claude-3-5-sonnet-latest``      → ``claude-3-5-sonnet``
      ``claude-3-5-sonnet-20241022``    → ``claude-3-5-sonnet``
      ``gpt-4o-2024-08-06``             → ``gpt-4o``
      ``gpt-4o-mini-2024-07-18``        → ``gpt-4o-mini``
      ``gpt-4-32k``                     → ``gpt-4-32k`` (unchanged — different SKU)

    Matches against ``prices`` (the effective table incl. any overlay) so a
    ``SOCTALK_MODEL_PRICES`` entry for a self-hosted model is honoured too.
    If the stripped result doesn't exactly match a key, the caller falls
    through to the configured unknown-model fallback. Fail-closed by default:
    an unrecognized variant gets the conservative price so the dollar cap
    halts early rather than billing a $30/MTok model at $3 on a fuzzy prefix.
    """
    if not model:
        return ""
    table = prices if prices is not None else _MODEL_PRICES_PER_MTOK
    stripped = _VERSION_SUFFIX_RE.sub("", model, count=1)
    if stripped in table:
        return stripped
    # No suffix match — try the raw name in case the caller passed a
    # base ID already.
    if model in table:
        return model
    return model  # cost lookup will fall back to the unknown-model rate


def _token_budget_default() -> int:
    raw = os.getenv("SOCTALK_CASE_RUN_TOKEN_BUDGET")
    if not raw:
        return _DEFAULT_TOKEN_BUDGET
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_TOKEN_BUDGET
    return v if v > 0 else _DEFAULT_TOKEN_BUDGET


def _dollar_budget_default() -> float:
    raw = os.getenv("SOCTALK_CASE_RUN_DOLLAR_BUDGET")
    if not raw:
        return _DEFAULT_DOLLAR_BUDGET
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_DOLLAR_BUDGET
    return v if v > 0 else _DEFAULT_DOLLAR_BUDGET


def ensure(state: dict[str, Any]) -> None:
    state.setdefault("tokens_used", 0)
    state.setdefault("tokens_budget", _token_budget_default())
    state.setdefault("dollars_used", 0.0)
    state.setdefault("dollars_budget", _dollar_budget_default())


def extract_usage(response: Any) -> tuple[int, int]:
    """Return (input_tokens, output_tokens) from an LLM response.

    Handles langchain ``usage_metadata`` (both providers normalize into
    input_tokens/output_tokens; Anthropic folds cache read/creation tokens
    into input_tokens) and falls back to raw ``response_metadata`` shapes.
    Public: chat and any future call sites share this one extractor.
    """
    um = getattr(response, "usage_metadata", None)
    if isinstance(um, dict):
        return (
            int(um.get("input_tokens") or 0),
            int(um.get("output_tokens") or 0),
        )
    rm = getattr(response, "response_metadata", None)
    if isinstance(rm, dict):
        usage = rm.get("usage") or rm.get("token_usage") or {}
        if isinstance(usage, dict):
            return (
                int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            )
    return (0, 0)


def extract_cache_details(response: Any) -> tuple[int, int]:
    """Return (cache_read_tokens, cache_creation_tokens) from a response.

    langchain-anthropic folds cache tokens INTO input_tokens and exposes
    the split under ``input_token_details`` (cache_read / cache_creation);
    langchain-openai exposes ``cache_read`` there too (reads only).
    """
    um = getattr(response, "usage_metadata", None)
    if isinstance(um, dict):
        details = um.get("input_token_details") or {}
        if isinstance(details, dict):
            return (
                int(details.get("cache_read") or 0),
                int(details.get("cache_creation") or 0),
            )
    return (0, 0)


def _model_name(response: Any) -> str | None:
    """Pull the model identifier from the response (langchain populates this)."""
    rm = getattr(response, "response_metadata", None)
    if isinstance(rm, dict):
        for key in ("model_name", "model", "model_id"):
            v = rm.get(key)
            if isinstance(v, str) and v:
                return v
    return None



def _snapshot_rates(state: dict[str, Any], model: str | None) -> dict[str, float] | None:
    """Rates for ``model`` from the run's price snapshot, if it carries them.

    The snapshot is stamped when the run is created (#125) and keyed by ROLE,
    because a tenant can point the fast and reasoning roles at the same model
    string through different providers at different prices. Matching is by the
    model actually called, across roles; when both roles resolved to the same
    model the rates agree and the first match is correct either way.

    Returns None when there is no snapshot, no entry for this model, or the
    entry resolved ``unknown`` — all of which mean "fall through to the table",
    so a run created before this existed prices exactly as it did before.
    """
    snapshot = state.get("price_snapshot")
    if not isinstance(snapshot, dict) or not model:
        return None
    for entry in (snapshot.get("models") or {}).values():
        if not isinstance(entry, dict) or entry.get("model") != model:
            continue
        if entry.get("source") == "unknown":
            return None
        try:
            rates = {
                "input": float(entry["input_per_mtok"]),
                "output": float(entry["output_per_mtok"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
        for key, out in (
            ("cache_read_per_mtok", "cache_read"),
            ("cache_write_per_mtok", "cache_write"),
        ):
            if entry.get(key) is not None:
                try:
                    rates[out] = float(entry[key])
                except (TypeError, ValueError):
                    pass
        return rates
    return None


def _cost_dollars(
    input_tokens: int,
    output_tokens: int,
    model: str | None,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    rates: dict[str, float] | None = None,
) -> float:
    """Price a call. Cache tokens are a subset of input_tokens: reads bill
    at ~10% of the input rate, cache writes at 125% (Anthropic pricing
    model) — without this split, cached runs would be overcharged ~10x on
    exactly the tokens caching exists to make cheap."""
    # Rates carried on the run win: they were resolved from the catalog or the
    # tenant's own override when the run was created, and they are what this
    # run is accountable to no matter what the table says now.
    price = rates
    if price is None:
        prices = _effective_prices()
        normalized = _normalize_model(model, prices)
        price = prices.get(normalized)
    if price is None:
        # Unpriced model: apply the configured fallback and, when that's the
        # fail-expensive default (not an explicit deployment choice), surface
        # it once so mispricing is visible rather than a silent early halt.
        price, explicit = _unknown_model_cost()
        if not explicit:
            _warn_unpriced_once(model, price)
    cache_read_tokens = min(max(cache_read_tokens, 0), input_tokens)
    cache_creation_tokens = min(
        max(cache_creation_tokens, 0), input_tokens - cache_read_tokens
    )
    uncached_input = input_tokens - cache_read_tokens - cache_creation_tokens
    # Cache dimensions default to the Anthropic-shaped derivation (reads at 10%
    # of input, writes at 125%) unless the price carries explicit rates, which
    # a catalog entry may.
    cache_read_rate = price.get("cache_read", price["input"] * 0.1)
    cache_write_rate = price.get("cache_write", price["input"] * 1.25)
    return (
        (uncached_input / 1_000_000.0) * price["input"]
        + (cache_read_tokens / 1_000_000.0) * cache_read_rate
        + (cache_creation_tokens / 1_000_000.0) * cache_write_rate
        + (output_tokens / 1_000_000.0) * price["output"]
    )


def track(state: dict[str, Any], response: Any) -> int:
    """Accumulate token + dollar usage from a single LLM response.

    Returns the cumulative ``tokens_used`` for back-compat with the
    previous return shape; callers that need the dollar figure read
    ``state["dollars_used"]`` directly.
    """
    ensure(state)
    input_tokens, output_tokens = extract_usage(response)
    cache_read, cache_creation = extract_cache_details(response)
    delta_tokens = input_tokens + output_tokens
    model = _model_name(response)
    delta_dollars = _cost_dollars(
        input_tokens,
        output_tokens,
        model,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        rates=_snapshot_rates(state, model),
    )

    state["tokens_used"] = int(state["tokens_used"]) + delta_tokens
    state["dollars_used"] = float(state["dollars_used"]) + delta_dollars
    state["cache_read_tokens"] = int(state.get("cache_read_tokens", 0)) + cache_read
    state["cache_creation_tokens"] = (
        int(state.get("cache_creation_tokens", 0)) + cache_creation
    )

    if delta_tokens or delta_dollars:
        logger.debug(
            "llm_call_tracked",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            delta_dollars=round(delta_dollars, 6),
            tokens_used=state["tokens_used"],
            tokens_budget=state["tokens_budget"],
            dollars_used=round(state["dollars_used"], 4),
            dollars_budget=state["dollars_budget"],
        )
    return state["tokens_used"]


def over_budget(state: dict[str, Any]) -> bool:
    """True when EITHER the token cap OR the dollar cap is exceeded.

    Either-or rather than and-and: dollars is the load-bearing cap, but
    keeping the token check lets the existing 30k-token demo override
    still bite even when the model name isn't priced.
    """
    ensure(state)
    if int(state["tokens_used"]) >= int(state["tokens_budget"]):
        return True
    if float(state["dollars_used"]) >= float(state["dollars_budget"]):
        return True
    return False


def soft_warn_ratio() -> float:
    """Fraction of the per-run budget at which a SOFT warning fires (no halt).

    Default 0.75 (issue #103). ``SOCTALK_BUDGET_WARN_RATIO`` overrides it; a
    value outside (0, 1) is ignored (a warn at 0 or >=100% is meaningless — the
    latter is just the hard halt).
    """
    raw = os.getenv("SOCTALK_BUDGET_WARN_RATIO", "")
    if raw.strip():
        try:
            r = float(raw)
            if 0.0 < r < 1.0:
                return r
        except ValueError:
            pass
    return 0.75


def crossed_soft_warn(state: dict[str, Any]) -> bool:
    """True when spend crossed the soft-warn ratio but is NOT yet over budget.

    A run approaching its cap should be surfaced (panel / flight recorder)
    before it hard-halts at 100%. Either cap crossing the ratio warns.
    """
    ensure(state)
    if over_budget(state):
        return False  # the hard halt supersedes a soft warning
    r = soft_warn_ratio()
    if int(state["tokens_used"]) >= r * int(state["tokens_budget"]):
        return True
    if float(state["dollars_used"]) >= r * float(state["dollars_budget"]):
        return True
    return False


def reason(state: dict[str, Any]) -> str:
    """Human-readable explanation of which cap fired."""
    ensure(state)
    parts: list[str] = []
    if int(state["tokens_used"]) >= int(state["tokens_budget"]):
        parts.append(f"tokens={state['tokens_used']}/{state['tokens_budget']}")
    if float(state["dollars_used"]) >= float(state["dollars_budget"]):
        parts.append(
            f"dollars=${state['dollars_used']:.2f}/${state['dollars_budget']:.2f}"
        )
    return "; ".join(parts) if parts else "within budget"
