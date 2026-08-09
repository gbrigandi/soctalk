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
from soctalk.core.pricing.names import VERSION_SUFFIX_RE
from soctalk.core.pricing.usage import canonical_usage

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
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
    # Current families. Their absence is why normalizing alone was not enough:
    # claude-haiku-4-5-20251001 stripped correctly to claude-haiku-4-5 and then
    # still missed the table, billing at the $15/$75 fallback (#139).
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
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


# Retained so tests and any in-process caller can reset the module cleanly;
# the env overlay it used to hold is retired (#125).
_price_cache: tuple[str | None, dict[str, dict[str, float]]] | None = None


def _effective_prices() -> dict[str, dict[str, float]]:
    """The built-in table of shipped defaults.

    ``SOCTALK_MODEL_PRICES`` is retired (#125). It was the only way to price a
    model the built-in table had never heard of, and it was the wrong shape for
    the job: keyed by model string alone, when the same model costs different
    amounts at different providers; delivered by rendering into worker env, so
    correcting a price meant a helm upgrade and a pod restart; and invisible,
    so nobody could tell which rate a run had actually been billed at.

    Prices now resolve from the install catalog (and the tenant's own override)
    when a run is created, and ride on the run row, which fixes all three: the
    key carries the provider, a correction takes effect on the next run with no
    rollout, and the run says what it was priced at and where that came from.

    What remains here is the shipped default for the frontier models, used when
    a call carries no resolved price — an unstamped legacy run, or a code path
    with no tenant to resolve against.
    """
    return _MODEL_PRICES_PER_MTOK


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
        hint="add it to the price catalog (soctalk-prices import), set a tenant "
             "override, or SOCTALK_UNKNOWN_MODEL_COST=zero for local inference",
    )


# Shared with the catalog path so the two cannot drift (#139) — see
# ``core/pricing/names.py`` for why only known suffix shapes are stripped.
_VERSION_SUFFIX_RE = VERSION_SUFFIX_RE


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



# Returned when a run was resolved and the answer was "nobody knows this
# model's price". Distinct from ``None``, which means no snapshot applied at
# all — a legacy run, or a caller with no tenant — and which still falls back
# to the shipped defaults.
_UNKNOWN_SENTINEL: dict[str, float] = {}



def _snapshot_source(state: dict[str, Any], model: str | None) -> str | None:
    """Which entry priced this call, for the log line. Never raises."""
    snapshot = state.get("price_snapshot")
    if not isinstance(snapshot, dict) or not model:
        return None
    stripped = _VERSION_SUFFIX_RE.sub("", model, count=1)
    for entry in (snapshot.get("models") or {}).values():
        if not isinstance(entry, dict):
            continue
        candidate = str(entry.get("model") or "")
        if candidate == model or _VERSION_SUFFIX_RE.sub("", candidate, count=1) == stripped:
            return entry.get("source")
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

    # Providers answer with versioned ids (``-20250514``, ``-latest``) that the
    # configured model string does not carry, so compare on the same stripped
    # form the built-in table uses. Without this a snapshot for
    # ``deepseek-v4-flash`` is ignored the moment the API reports
    # ``deepseek-v4-flash-20260731``, and the call silently falls back to the
    # fail-expensive rate this feature exists to remove (Codex review, #3).
    # Strip unconditionally, unlike ``_normalize_model``, which only strips when
    # the result happens to be a key in the built-in table — that guard is right
    # for a table lookup and wrong here, where both sides are stripped and
    # compared to each other.
    wanted = _VERSION_SUFFIX_RE.sub("", model, count=1)
    matches: list[dict[str, Any]] = []
    for entry in (snapshot.get("models") or {}).values():
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("model")
        if not candidate:
            continue
        if candidate != model and _VERSION_SUFFIX_RE.sub(
            "", str(candidate), count=1
        ) != wanted:
            continue
        matches.append(entry)

    if not matches:
        return None

    # A stamp that says "unknown" must not be quietly rescued by the shipped
    # defaults. If it were, the run would record one story and be billed by
    # another, and "why was this run priced this way" would have a wrong
    # answer even when the number happened to be right (Codex).
    if all(e.get("source") == "unknown" for e in matches):
        return _UNKNOWN_SENTINEL

    rated = [_rates_from_entry(e) for e in matches]
    rated = [r for r in rated if r is not None]
    if not rated:
        # Matched, but the entry was ``unknown`` or unreadable. Fall through to
        # the table rather than pricing at zero, which would hide the gap.
        return None
    first = rated[0]
    if any(other != first for other in rated[1:]):
        # Two roles resolved the same model string to different rates, which
        # happens when a hybrid tenant runs one model through two providers.
        # The call site does not tell us which role is spending, so guessing
        # would bill half the calls at the wrong provider's rate. Fall through
        # and say so (Codex review, #2).
        logger.warning(
            "price_snapshot_ambiguous",
            model=model,
            hint="model appears under multiple roles at different rates; "
                 "priced from the table instead",
        )
        return None
    return first


def _rates_from_entry(entry: dict[str, Any]) -> dict[str, float] | None:
    """One snapshot entry as rates, or None if it cannot price anything."""
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
        ("reasoning_per_mtok", "reasoning"),
    ):
        if entry.get(key) is not None:
            try:
                rates[out] = float(entry[key])
            except (TypeError, ValueError):
                pass
    return rates


def _cost_dollars(
    input_tokens: int,
    output_tokens: int,
    model: str | None,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    reasoning_tokens: int = 0,
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
    if price is _UNKNOWN_SENTINEL:
        # Resolved, and the answer was unknown. Go straight to the configured
        # unknown-model policy rather than the shipped defaults, so the run is
        # billed by the story it recorded.
        price = None
        rates = None
        prices = None
    elif price is None:
        prices = _effective_prices()
        normalized = _normalize_model(model, prices)
        price = prices.get(normalized)
    if not price:
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
    # Reasoning tokens are a SUBSET of output. OpenAI, Anthropic, Gemini and
    # DeepSeek all bill them at the output rate today, so by default the split
    # costs exactly what it did before. A price naming a separate reasoning
    # rate (OpenRouter exposes one) applies it to that subset only.
    reasoning_tokens = min(max(reasoning_tokens, 0), output_tokens)
    reasoning_rate = price.get("reasoning", price["output"])
    plain_output = output_tokens - reasoning_tokens
    return (
        (uncached_input / 1_000_000.0) * price["input"]
        + (cache_read_tokens / 1_000_000.0) * cache_read_rate
        + (cache_creation_tokens / 1_000_000.0) * cache_write_rate
        + (plain_output / 1_000_000.0) * price["output"]
        + (reasoning_tokens / 1_000_000.0) * reasoning_rate
    )


def track(state: dict[str, Any], response: Any) -> int:
    """Accumulate token + dollar usage from a single LLM response.

    Returns the cumulative ``tokens_used`` for back-compat with the
    previous return shape; callers that need the dollar figure read
    ``state["dollars_used"]`` directly.
    """
    ensure(state)
    usage = canonical_usage(response)
    # The legacy extractors stay authoritative for the totals, so a provider
    # shape the normaliser has not learned cannot regress token accounting.
    input_tokens, output_tokens = extract_usage(response)
    if not input_tokens and not output_tokens:
        input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
    legacy_read, legacy_write = extract_cache_details(response)
    cache_read = usage.cache_read_tokens or legacy_read
    cache_creation = usage.cache_write_tokens or legacy_write
    delta_tokens = input_tokens + output_tokens
    model = _model_name(response)
    if usage.actual_cost_usd is not None:
        # The provider said what it charged. No estimate beats that, and
        # recording it is what turns drift detection into a real comparison
        # rather than our arithmetic against our own rate card.
        delta_dollars = usage.actual_cost_usd
        state["cost_basis"] = "provider_reported"
    else:
        delta_dollars = _cost_dollars(
            input_tokens,
            output_tokens,
            model,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            reasoning_tokens=usage.reasoning_tokens,
            rates=_snapshot_rates(state, model),
        )
        state.setdefault("cost_basis", "estimated")
    # Which rate card produced the number. Kept on state rather than computed
    # only for the log line, so the worker can report it and the ledger can
    # store it — "a figure nobody can attribute" was the problem, and a value
    # that exists for one log call and then vanishes does not fix it.
    if usage.actual_cost_usd is not None:
        # No rate card was consulted: the provider said what it charged.
        state["price_source"] = "provider"
    else:
        state["price_source"] = _snapshot_source(state, model) or "unknown"

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
            reasoning_tokens=usage.reasoning_tokens,
            # Which of the two produced this number, and where the rate came
            # from. Recording the basis is the point of the whole exercise: a
            # figure nobody can attribute is the state we started in.
            cost_basis=state.get("cost_basis"),
            price_source=state.get("price_source"),
            delta_dollars=round(delta_dollars, 6),
            tokens_used=state["tokens_used"],
            tokens_budget=state["tokens_budget"],
            dollars_used=round(state["dollars_used"], 4),
            dollars_budget=state["dollars_budget"],
        )
    return state["tokens_used"]


def cost_tracking_off(state: dict[str, Any]) -> bool:
    """True when this run's tenant has dollar accounting turned off.

    Carried on the run's price snapshot, which is stamped at creation and
    already reaches the worker — so the answer travels with the run rather than
    needing a database read per budget check, and a mid-run policy change
    cannot alter how an in-flight run is enforced.
    """
    snapshot = state.get("price_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("cost_tracking") is False:
        return True
    return False


def over_budget(state: dict[str, Any]) -> bool:
    """True when EITHER the token cap OR the dollar cap is exceeded.

    Either-or rather than and-and: dollars is the load-bearing cap, but
    keeping the token check lets the existing 30k-token demo override
    still bite even when the model name isn't priced.

    With cost tracking off the dollar half is skipped entirely: the figure it
    would compare against is an estimate the operator has said not to keep, and
    halting a run on it would be enforcing a number nobody is counting. Tokens
    still bound the work — they are measured, not inferred.
    """
    ensure(state)
    if int(state["tokens_used"]) >= int(state["tokens_budget"]):
        return True
    if cost_tracking_off(state):
        return False
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


def _fmt_dollars(value: float) -> str:
    """Format a dollar amount without rounding a real number away to $0.00.

    Two decimals is right for a $5 budget and useless for a $0.0005 one: a run
    that halted on a sub-cent cap logged ``dollars=$0.00/$0.00``, which tells an
    operator nothing about why it stopped. Precision follows magnitude so the
    figure stays legible at both ends.
    """
    v = abs(value)
    if v and v < 0.01:
        return f"${value:.6f}"
    return f"${value:.2f}"


def reason(state: dict[str, Any]) -> str:
    """Human-readable explanation of which cap fired."""
    ensure(state)
    parts: list[str] = []
    if int(state["tokens_used"]) >= int(state["tokens_budget"]):
        parts.append(f"tokens={state['tokens_used']}/{state['tokens_budget']}")
    if float(state["dollars_used"]) >= float(state["dollars_budget"]):
        parts.append(
            f"dollars={_fmt_dollars(float(state['dollars_used']))}"
            f"/{_fmt_dollars(float(state['dollars_budget']))}"
        )
    return "; ".join(parts) if parts else "within budget"
