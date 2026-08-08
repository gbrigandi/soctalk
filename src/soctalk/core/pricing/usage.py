"""Normalising what a provider says it used, before anything is priced (#125).

Providers report usage in shapes that disagree in ways that cost money:

- OpenAI-compatible endpoints put prompt caching under
  ``prompt_tokens_details.cached_tokens`` and thinking under
  ``completion_tokens_details.reasoning_tokens``;
- Anthropic (through LangChain) puts the cache split under
  ``input_token_details.cache_read`` / ``cache_creation``;
- gateways add their own fields, and OpenRouter adds the actual cost.

Reading only ``prompt_tokens`` and ``completion_tokens``, which is what this
codebase did, bills every cached token at the full input rate on exactly the
providers where caching is meant to make calls cheap, and loses the reasoning
split entirely. On a measured call, 16 of 17 completion tokens were reasoning
tokens.

Everything lands in one ``CanonicalUsage`` so the pricing path never has to
know whose response it is looking at. The boundary is deliberate: pricing takes
canonical usage and an explicit price, and discovers nothing for itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CanonicalUsage:
    """One LLM call's usage, in terms every provider can be mapped onto.

    ``cache_read_tokens`` and ``cache_write_tokens`` are a subset of
    ``input_tokens``, and ``reasoning_tokens`` a subset of ``output_tokens``,
    matching how providers report them: the totals already include the parts.
    Pricing subtracts rather than adds, so a provider that omits the detail
    blocks degrades to today's behaviour instead of double counting.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # What the provider says the call actually cost, when it says so at all.
    # OpenRouter returns this inline on ``usage.cost`` when the request asks
    # for it; most providers never do. An actual is worth more than any
    # estimate we can compute, so pricing prefers it and records that it did.
    actual_cost_usd: float | None = None
    # Which upstream really served this, where the gateway discloses it.
    # OpenRouter names it at the top level of the response body, so it reaches
    # us from raw-dict callers (the eval and benchmark harnesses) but NOT
    # through langchain-openai, which keeps only its own ``model_provider``
    # and drops fields it does not model. None therefore means "not disclosed
    # to us here", never "single-vendor".
    reported_provider: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _as_int(value: Any) -> int:
    try:
        out = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, out)


def _usage_dicts(response: Any) -> list[dict[str, Any]]:
    """Every place a provider might have put its usage block, in order."""
    out: list[dict[str, Any]] = []
    um = getattr(response, "usage_metadata", None)
    if isinstance(um, dict):
        out.append(um)
    rm = getattr(response, "response_metadata", None)
    if isinstance(rm, dict):
        for key in ("usage", "token_usage"):
            block = rm.get(key)
            if isinstance(block, dict):
                out.append(block)
        # Gateways sometimes hang the served-provider name off the metadata
        # rather than the usage block.
        out.append({k: v for k, v in rm.items() if k in ("provider", "model_name")})
    if isinstance(response, dict):
        block = response.get("usage")
        if isinstance(block, dict):
            out.append(block)
        # OpenRouter names the upstream it routed to at the top level of the
        # body, beside ``choices``, not inside the usage block.
        if isinstance(response.get("provider"), str):
            out.append({"provider": response["provider"]})
    return out


def canonical_usage(response: Any) -> CanonicalUsage:
    """Read one response into ``CanonicalUsage``, whatever shape it arrived in.

    Never raises: a response that carries no usage at all yields zeros, which
    prices to nothing and is the honest answer — an accounting layer must not
    be able to break the call it is accounting for.
    """
    usage = CanonicalUsage()
    for block in _usage_dicts(response):
        if not isinstance(block, dict):
            continue

        # Totals. LangChain normalises to input_/output_tokens; the raw
        # OpenAI shape uses prompt_/completion_tokens.
        for key in ("input_tokens", "prompt_tokens"):
            if block.get(key) is not None:
                usage.input_tokens = usage.input_tokens or _as_int(block[key])
        for key in ("output_tokens", "completion_tokens"):
            if block.get(key) is not None:
                usage.output_tokens = usage.output_tokens or _as_int(block[key])

        # OpenAI-compatible detail blocks. ``cache_write_tokens`` is an
        # OpenRouter extension; absent elsewhere, which is why it defaults to 0
        # rather than being derived.
        prompt_details = block.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            usage.cache_read_tokens = usage.cache_read_tokens or _as_int(
                prompt_details.get("cached_tokens")
            )
            usage.cache_write_tokens = usage.cache_write_tokens or _as_int(
                prompt_details.get("cache_write_tokens")
            )
        completion_details = block.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            usage.reasoning_tokens = usage.reasoning_tokens or _as_int(
                completion_details.get("reasoning_tokens")
            )

        # Anthropic / LangChain shape.
        input_details = block.get("input_token_details")
        if isinstance(input_details, dict):
            usage.cache_read_tokens = usage.cache_read_tokens or _as_int(
                input_details.get("cache_read")
            )
            usage.cache_write_tokens = usage.cache_write_tokens or _as_int(
                input_details.get("cache_creation")
            )
        output_details = block.get("output_token_details")
        if isinstance(output_details, dict):
            usage.reasoning_tokens = usage.reasoning_tokens or _as_int(
                output_details.get("reasoning") or output_details.get("thinking_tokens")
            )

        # An actual, where the provider gives one. Validated before it is
        # trusted: this figure OVERRIDES our own arithmetic and is what budget
        # enforcement compares against, so a negative, NaN or infinite value
        # from a provider would under-bill or stop over_budget from ever
        # tripping (Codex review, finding 7). A bad number is discarded, which
        # degrades to our estimate rather than to no ceiling at all.
        if usage.actual_cost_usd is None and block.get("cost") is not None:
            try:
                cost = float(block["cost"])
            except (TypeError, ValueError):
                cost = None
            if cost is not None and cost == cost and cost >= 0.0 and cost != float("inf"):
                usage.actual_cost_usd = cost
        if usage.reported_provider is None and isinstance(block.get("provider"), str):
            usage.reported_provider = block["provider"]

    # The detail blocks are subsets of the totals, so they cannot exceed them;
    # a provider that reports otherwise is not a reason to bill for tokens that
    # were never counted.
    usage.cache_read_tokens = min(usage.cache_read_tokens, usage.input_tokens)
    usage.cache_write_tokens = min(
        usage.cache_write_tokens, max(0, usage.input_tokens - usage.cache_read_tokens)
    )
    usage.reasoning_tokens = min(usage.reasoning_tokens, usage.output_tokens)
    return usage
