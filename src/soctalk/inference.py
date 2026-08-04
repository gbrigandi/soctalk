"""Unified structured-inference request abstraction (issue #32).

An LLM call has two sides. On the OUTPUT side, structured inference means the
model returns a value conforming to a schema (constrained via tool-use,
json-schema, or a served engine's guided decoding) — machine-consumable by
construction, or it fails loudly. On the INPUT side, the request is a typed
envelope carrying everything the call needs — tier, output schema, decoding
mode, sampling, tools, accounting — dispatched through ONE seam and funnelled
through the accounting that already exists (``graph/budget.track``).

This is the foundation the rest of the inference-alignment program builds on:
per-tier providers (#4), self-hosted serving via the decoding-mode seam (#13),
the chat agent (#10), and the compatibility harness (#9). ``llm.py`` stays the
provider factory; this module is the envelope + resolver + dispatcher.

Design note (research-grounded): schema enforcement is applied to the
EXTRACTION, never the reasoning. ``ExtractionPolicy.REASON_THEN_EXTRACT`` runs
an unconstrained reasoning call then a constrained extraction over its output
— "Let Me Speak Freely?" / dottxt "Say What You Mean" agree the harm is
premature serialization, not JSON.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, cast
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage

from soctalk.config import LLMConfig
from soctalk.llm import (
    SchemaValidationError,
    ServerlessUnavailableError,
    create_chat_model,
    make_system_message,
)

T = TypeVar("T")

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- enums


class InferenceTier(str, Enum):
    ROUTER = "router"
    REASONING = "reasoning"
    CHAT = "chat"
    EXTRACTION = "extraction"


class ProviderEngine(str, Enum):
    FRONTIER = "frontier"                    # hosted Anthropic / OpenAI
    OPENAI_COMPATIBLE = "openai_compatible"  # generic gateway
    VLLM = "vllm"
    SGLANG = "sglang"


class DecodingMode(str, Enum):
    AUTO = "auto"
    NONE = "none"
    TOOL_USE = "tool_use"
    JSON_SCHEMA_STRICT = "json_schema_strict"
    # response_format={"type":"json_object"} + the schema described in the
    # prompt. For OpenAI-compatible endpoints that reject strict json_schema AND
    # tool_choice — notably DeepSeek's hosted thinking models (deepseek-v4-flash:
    # "response_format type unavailable" / "Thinking mode does not support this
    # tool_choice"). Weaker than strict (schema isn't enforced by the API, so the
    # validation retry earns its keep), but the only structured path they accept.
    JSON_OBJECT = "json_object"
    GUIDED_JSON = "guided_json"
    GUIDED_GRAMMAR = "guided_grammar"


class ExtractionPolicy(str, Enum):
    SINGLE_CALL = "single_call"
    REASON_THEN_EXTRACT = "reason_then_extract"


# ------------------------------------------------------------- envelope types


@dataclass(frozen=True)
class SamplingParams:
    temperature: float = 0.0
    max_tokens: int = 1024


@dataclass(frozen=True)
class ToolSpec:
    """A tool binding passthrough (chat is the first real consumer, #10)."""

    tool: Any


@dataclass
class InferenceAccounting:
    """Where the response's usage is funnelled + who asked. ``budget_state``
    is the dict ``graph/budget.track`` reads usage into (the single accounting
    seam, #7); ``None`` means don't track (e.g. offline/eval calls)."""

    producer: str
    budget_state: dict[str, Any] | None = None
    investigation_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None


@dataclass
class InferenceRequest:
    tier: InferenceTier
    metadata: InferenceAccounting
    decoding_mode: DecodingMode = DecodingMode.AUTO
    extraction_policy: ExtractionPolicy = ExtractionPolicy.SINGLE_CALL
    output_schema: type | None = None
    grammar: str | None = None
    # ``system`` is separate from ``messages`` so the dispatcher preserves the
    # provider-aware prompt-cache behaviour in make_system_message (Anthropic
    # block-form cache_control; OpenAI plain text).
    system: str | None = None
    messages: list[Any] = field(default_factory=list)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: str | None = "auto"
    retry_schema_validation: int = 1
    model_override: str | None = None


@dataclass(frozen=True)
class ResolvedModel:
    tier: InferenceTier
    provider: str            # 'anthropic' | 'openai'
    engine: ProviderEngine
    model: str
    decoding_mode: DecodingMode
    llm_config: LLMConfig    # provider-scoped: only the chosen provider's key


@dataclass(frozen=True)
class UsageDelta:
    input_tokens: int
    output_tokens: int


# ------------------------------------------------ model-consumption abstraction
# (#63) A backend is a delivery model, not just a wire protocol. The Delivery
# profile declares the per-backend semantics the scheduler and cost layers need
# (readiness, scaling, billing, batching, capabilities) so InferenceRequest stays
# uniform. Frontier and OpenAI-compatible (Modal served, RunPod pod, Ollama) run
# through the same sync-chat driver here; the RunPod async-job driver and the
# serverless scheduling/cost work land in #64.


class BackendKind(str, Enum):
    FRONTIER = "frontier"            # hosted Anthropic / OpenAI
    OPENAI_COMPAT = "openai_compat"  # generic OpenAI-compatible gateway
    MODAL = "modal"                  # self-deployed serverless GPU (OpenAI-compat)
    RUNPOD_POD = "runpod_pod"        # always-on RunPod VM (OpenAI-compat)
    RUNPOD_JOB = "runpod_job"        # RunPod serverless async-job API (#64)
    OLLAMA = "ollama"                # local single-node


@dataclass(frozen=True)
class CapabilitySet:
    tools: bool
    strict_json: bool
    guided_json: bool
    grammar: bool
    streaming: bool
    prompt_cache: bool
    batch_api: bool


@dataclass(frozen=True)
class DeliveryProfile:
    backend_id: str
    kind: BackendKind
    invocation: str   # sync_chat | async_job | batch_api
    readiness: str    # warm | scale_to_zero | local_load
    lifecycle: str    # managed | self_deployed | local
    scaling: str      # provider | fixed | scale_to_zero | single_instance
    billing: str      # per_token | per_gpu_second | free
    batching: str     # provider_managed | continuous_worker | none
    capabilities: CapabilitySet


@dataclass(frozen=True)
class UsageRecord:
    """Backend-agnostic usage/cost record (#63). Tokens are always populated;
    the timing and GPU-second fields are filled by the serverless drivers in
    #64 and stay None on the sync-chat path."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    backend_id: str
    backend_kind: str
    provider_job_id: str | None = None
    queue_seconds: float | None = None
    cold_start_seconds: float | None = None
    compute_seconds: float | None = None
    billable_seconds: float | None = None
    estimated_dollars: float | None = None


@dataclass(frozen=True)
class ResolvedBackend:
    resolved: ResolvedModel
    profile: DeliveryProfile


@dataclass
class InferenceResult(Generic[T]):
    parsed: T | None
    raw_message: Any
    text: str | None
    tool_calls: list[Any]
    usage: UsageDelta
    resolved: ResolvedModel
    attempts: int
    parsing_error: str | None = None
    # #63: richer, backend-tagged usage; None on legacy call paths.
    usage_record: UsageRecord | None = None


# ------------------------------------------------------------- tier resolution


def _legacy_model_for_tier(cfg: LLMConfig, tier: InferenceTier) -> str:
    if tier == InferenceTier.REASONING:
        return cfg.reasoning_model
    if tier == InferenceTier.CHAT:
        return getattr(cfg, "chat_model", "") or cfg.fast_model
    # ROUTER / EXTRACTION
    return cfg.fast_model


def _scoped_config(cfg: LLMConfig, provider: str, base_url: str | None,
                   key: str | None) -> LLMConfig:
    """A per-tier LLMConfig carrying ONLY the chosen provider's key, so
    create_chat_model's both-keys guard never trips even when the global
    config has keys for multiple providers (the multi-provider seam #4 needs).
    """
    data = cfg.model_dump()
    data["provider"] = provider
    if provider == "anthropic":
        data["anthropic_api_key"] = key or cfg.anthropic_api_key
        data["anthropic_base_url"] = base_url or cfg.anthropic_base_url
        # Scrub the other provider entirely so create_chat_model's both-keys
        # guard can't trip and no stale base_url leaks across providers.
        data["openai_api_key"] = ""
        data["openai_base_url"] = None
    else:
        data["openai_api_key"] = key or cfg.openai_api_key
        data["openai_base_url"] = base_url or cfg.openai_base_url
        data["anthropic_api_key"] = ""
        data["anthropic_base_url"] = None
    return LLMConfig(**data)


def resolve_tier(
    cfg: LLMConfig, tier: InferenceTier, *, model_override: str | None = None,
) -> ResolvedModel:
    """Resolve a tier to provider/engine/model/decoding, overlaying the
    optional per-tier config field-by-field on legacy defaults.

    Legacy defaults preserve current behaviour: router/extraction -> global
    provider + fast_model; reasoning -> reasoning_model; chat -> chat_model.
    """
    tiers = getattr(cfg, "tiers", None) or {}
    tconf = tiers.get(tier.value) or tiers.get(tier) or {}

    explicit_provider = tconf.get("provider")
    engine_raw = tconf.get("engine")
    if engine_raw:
        engine = ProviderEngine(engine_raw)
    else:
        base_provider = explicit_provider or cfg.provider
        engine = (ProviderEngine.FRONTIER if base_provider in ("anthropic", "openai")
                  else ProviderEngine.OPENAI_COMPATIBLE)
    # A served / generic OpenAI-compatible engine (vLLM, SGLang, a gateway) speaks
    # the OpenAI protocol, so it must use the OpenAI client even when the global
    # provider is Anthropic — otherwise a served-engine tier would build the wrong
    # client. An explicit per-tier provider always wins.
    if explicit_provider:
        provider = explicit_provider
    elif engine in (ProviderEngine.VLLM, ProviderEngine.SGLANG,
                    ProviderEngine.OPENAI_COMPATIBLE):
        provider = "openai"
    else:
        provider = cfg.provider
    model = model_override or tconf.get("model") or _legacy_model_for_tier(cfg, tier)
    decoding = DecodingMode(tconf.get("default_decoding_mode", "auto"))

    scoped = _scoped_config(
        cfg, provider,
        base_url=tconf.get("base_url"),
        key=tconf.get("api_key"),
    )
    return ResolvedModel(
        tier=tier, provider=provider, engine=engine, model=model,
        decoding_mode=decoding, llm_config=scoped,
    )


def resolve_tier_sampling(
    cfg: Any, tier: InferenceTier, *, temperature: float, max_tokens: int,
) -> SamplingParams:
    """Per-tier sampling overlay (issue #4 follow-up).

    A configured tier may override the caller's default temperature / max_tokens
    (``SOCTALK_<TIER>_TEMPERATURE`` / ``_MAX_TOKENS`` → ``cfg.tiers[tier]``). When
    a tier omits a field the caller default wins — the router default is the
    tenant-global sampling, the reasoning default the verdict's tuned constants —
    so single-provider tenants (empty ``tiers``) behave exactly as before. The
    per-tier values are already coerced + bounded at config load.
    """
    tiers = getattr(cfg, "tiers", None) or {}
    tconf = tiers.get(tier.value) or tiers.get(tier) or {}
    return SamplingParams(
        temperature=tconf.get("temperature", temperature),
        max_tokens=tconf.get("max_tokens", max_tokens),
    )


# --------------------------------------------------------- decoding-mode seam


def resolve_decoding_mode(
    requested: DecodingMode, *, engine: ProviderEngine, provider: str,
    has_schema: bool, has_grammar: bool,
) -> DecodingMode:
    """Resolve AUTO to a concrete mechanism once provider/engine is known
    (the #13 seam). Rejects modes an engine can't honour rather than
    silently degrading."""
    if has_schema and has_grammar:
        # A request carries one constrained-decoding target, not both — an
        # engine honours a single constraint per call, and silently preferring
        # one would drop the other.
        raise ValueError(
            "output_schema and grammar are mutually exclusive on one request"
        )
    if requested != DecodingMode.AUTO:
        # Validate the explicit request against the engine.
        if requested in (DecodingMode.GUIDED_JSON, DecodingMode.GUIDED_GRAMMAR) \
                and engine in (ProviderEngine.FRONTIER,):
            raise ValueError(f"{requested.value} not available on {engine.value}")
        if requested == DecodingMode.JSON_SCHEMA_STRICT and provider == "anthropic":
            # Anthropic has no json-schema response_format; use tool_use.
            return DecodingMode.TOOL_USE
        return requested

    # AUTO resolution
    if not has_schema and not has_grammar:
        return DecodingMode.NONE
    if engine in (ProviderEngine.VLLM, ProviderEngine.SGLANG):
        return DecodingMode.GUIDED_GRAMMAR if has_grammar else DecodingMode.GUIDED_JSON
    if has_grammar:
        # Only served engines (vLLM/SGLang) can honour a raw grammar; a
        # frontier API can't, so don't silently degrade to a schema-less mode.
        raise ValueError(f"grammar decoding not available on {engine.value}")
    if provider == "anthropic":
        return DecodingMode.TOOL_USE
    # OpenAI frontier
    return DecodingMode.JSON_SCHEMA_STRICT if has_schema else DecodingMode.TOOL_USE


# ------------------------------------------------------------- the dispatcher


def _build_messages(req: InferenceRequest, resolved: ResolvedModel) -> list[Any]:
    msgs: list[Any] = []
    if req.system is not None:
        msgs.append(make_system_message(req.system, resolved.llm_config))
    msgs.extend(req.messages)
    return msgs


def _usage_of(raw: Any) -> UsageDelta:
    from soctalk.graph.budget import extract_usage
    i, o = extract_usage(raw)
    return UsageDelta(input_tokens=i, output_tokens=o)


def _track(req: InferenceRequest, raw: Any) -> None:
    if raw is not None and req.metadata.budget_state is not None:
        from soctalk.graph.budget import track
        track(req.metadata.budget_state, raw)


def _json_object_hint(schema: type) -> HumanMessage:
    """Describe the target schema for a json_object-mode call — the API doesn't
    enforce the schema, so the model needs it in the prompt."""
    import json
    props = getattr(schema, "model_json_schema", lambda: {})().get("properties", {})
    return HumanMessage(content=(
        "Respond with ONLY a single JSON object (no prose, no markdown fence) "
        f"matching the {getattr(schema, '__name__', 'output')} schema — these "
        f"fields: {json.dumps(props, ensure_ascii=False)}"
    ))


async def _invoke_structured(
    llm: Any, schema: type, messages: list[Any], req: InferenceRequest,
    *, mode: DecodingMode = None,  # type: ignore[assignment]
) -> tuple[Any, Any, str | None, int]:
    """Schema-enforced invoke with one validation retry (the ainvoke_structured
    logic, inlined so the dispatcher owns tracking + attempt counting).
    Returns (parsed, raw, parsing_error, attempts)."""
    if mode == DecodingMode.JSON_OBJECT:
        # response_format=json_object (no strict schema); schema goes in the prompt.
        structured = llm.with_structured_output(schema, method="json_mode", include_raw=True)
        messages = [*messages, _json_object_hint(schema)]
    else:
        structured = llm.with_structured_output(schema, include_raw=True)
    attempts = 0

    result = await structured.ainvoke(messages)
    attempts += 1
    raw = result.get("raw")
    _track(req, raw)
    if result.get("parsed") is not None:
        return result["parsed"], raw, None, attempts

    for _ in range(max(0, req.retry_schema_validation)):
        parsing_error = result.get("parsing_error")
        feedback = (
            f"Your previous response failed validation against the "
            f"{getattr(schema, '__name__', 'output')} schema: {parsing_error}. "
            "Respond again, following the schema exactly."
        )
        retry = list(messages)
        if raw is not None:
            retry.append(raw)
            # Structured output rides on tool calling: an assistant turn carrying
            # tool_use blocks MUST be followed by matching tool_result blocks —
            # newer Claude models hard-reject the request otherwise. Deliver the
            # feedback as the tool result(s); invalid_tool_calls covers calls
            # truncated mid-JSON by max_tokens.
            tool_calls = (getattr(raw, "tool_calls", None) or []) + (
                getattr(raw, "invalid_tool_calls", None) or []
            )
            if tool_calls:
                from langchain_core.messages import ToolMessage

                retry.extend(
                    ToolMessage(content=feedback, tool_call_id=tc["id"]) for tc in tool_calls
                )
            else:
                retry.append(HumanMessage(content=feedback))
        else:
            retry.append(HumanMessage(content=feedback))
        result = await structured.ainvoke(retry)
        attempts += 1
        raw = result.get("raw")
        _track(req, raw)
        if result.get("parsed") is not None:
            return result["parsed"], raw, None, attempts

    raise SchemaValidationError(str(result.get("parsing_error")))


# --------------------------------------------- served-engine guided decoding


def _json_schema_of(schema: type) -> dict[str, Any]:
    fn = getattr(schema, "model_json_schema", None)
    if fn is None:
        raise TypeError(f"{schema!r} is not a pydantic model (no model_json_schema)")
    return cast("dict[str, Any]", fn())


def _parse_json_into(schema: type, text: str) -> Any:
    fn = getattr(schema, "model_validate_json", None)
    if fn is None:
        raise TypeError(f"{schema!r} is not a pydantic model (no model_validate_json)")
    return fn(text)


def guided_request_kwargs(
    mode: DecodingMode, engine: ProviderEngine, *,
    schema: dict[str, Any] | None = None, grammar: str | None = None,
    schema_name: str = "output",
) -> dict[str, Any]:
    """Shape a served-engine guided-decoding request into the kwargs to
    ``.bind()`` onto the OpenAI-compatible client.

    This is the per-engine wire seam. SGLang and vLLM both serve an
    OpenAI-compatible endpoint but carry constraints differently, and the
    contract drifts by version (vLLM moved the top-level ``guided_json`` fields
    under a ``structured_outputs`` object at v0.12). JSON-schema goes through the
    standard ``response_format`` where the engine honours it; a raw EBNF grammar
    is engine-native and rides ``extra_body``.
    """
    if engine == ProviderEngine.SGLANG:
        if mode == DecodingMode.GUIDED_JSON:
            if schema is None:
                raise ValueError("guided_json requires a schema")
            return {"response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }}
        if mode == DecodingMode.GUIDED_GRAMMAR:
            if not grammar:
                raise ValueError("guided_grammar requires an EBNF grammar")
            return {"extra_body": {"ebnf": grammar}}
    elif engine == ProviderEngine.VLLM:
        # vLLM >= 0.12 nests structured outputs; older builds used top-level
        # guided_json / guided_grammar. We target the current contract.
        if mode == DecodingMode.GUIDED_JSON:
            if schema is None:
                raise ValueError("guided_json requires a schema")
            return {"extra_body": {"structured_outputs": {"json": schema}}}
        if mode == DecodingMode.GUIDED_GRAMMAR:
            if not grammar:
                raise ValueError("guided_grammar requires an EBNF grammar")
            return {"extra_body": {"structured_outputs": {"grammar": grammar}}}
    raise ValueError(
        f"{mode.value} guided decoding is not wired for engine {engine.value}"
    )


async def _invoke_guided(
    req: InferenceRequest, resolved: ResolvedModel, mode: DecodingMode,
) -> InferenceResult[Any]:
    """Native served-engine guided decoding (SGLang / vLLM). Constraints ride
    the OpenAI-compatible client via ``response_format`` (JSON schema) or
    ``extra_body`` (EBNF grammar); the engine enforces them at decode time, so
    valid output is guaranteed by construction and we parse without a retry."""
    # Guided decoding rides the OpenAI-compatible client. A served-engine tier
    # that resolved to a non-openai provider (e.g. an explicit provider=anthropic
    # with engine=sglang) can't honour these kwargs — fail rather than send
    # SGLang/vLLM shaping to ChatAnthropic.
    if resolved.provider != "openai":
        raise ValueError(
            f"guided decoding requires the OpenAI-compatible client; tier resolved "
            f"to provider={resolved.provider!r} with engine={resolved.engine.value}"
        )
    # Combining tools with a constrained-output request is engine-specific and
    # SGLang forbids tools + response_format in one call. Refuse rather than
    # silently drop the tools (they are never bound on this path).
    if req.tools:
        raise ValueError(
            "tools are not supported together with guided decoding; issue the "
            "tool call and the constrained response as separate requests"
        )

    schema_dict: dict[str, Any] | None = None
    schema_name = "output"
    if req.output_schema is not None:
        schema_name = getattr(req.output_schema, "__name__", "output")
        schema_dict = _json_schema_of(req.output_schema)

    kwargs = guided_request_kwargs(
        mode, resolved.engine,
        schema=schema_dict, grammar=req.grammar, schema_name=schema_name,
    )

    llm = create_chat_model(
        resolved.llm_config, model=resolved.model,
        temperature=req.sampling.temperature, max_tokens=req.sampling.max_tokens,
    ).bind(**kwargs)

    messages = _build_messages(req, resolved)
    raw = await llm.ainvoke(messages)
    _track(req, raw)
    content = getattr(raw, "content", None)
    text = content if isinstance(content, str) else None

    parsed: Any = None
    # GUIDED_JSON returns schema-conforming JSON in the content; parse it into
    # the pydantic model. The engine enforces validity at decode time, so a
    # failure here is a real contract break — raise loudly for parity with the
    # frontier structured path, never return a silent parsed=None that callers
    # would treat as a valid decision. GUIDED_GRAMMAR returns raw grammar text.
    if mode == DecodingMode.GUIDED_JSON and req.output_schema is not None:
        if not isinstance(text, str):
            raise SchemaValidationError(
                f"guided_json returned non-text content: {content!r}"
            )
        try:
            parsed = _parse_json_into(req.output_schema, text)
        except Exception as e:  # noqa: BLE001 — re-raise as the canonical error
            raise SchemaValidationError(
                f"guided_json output failed {schema_name} validation: {e}"
            ) from e

    return InferenceResult(
        parsed=parsed, raw_message=raw, text=text,
        tool_calls=getattr(raw, "tool_calls", []) or [],
        usage=_usage_of(raw),
        resolved=replace(resolved, decoding_mode=mode), attempts=1,
    )


async def _dispatch(
    req: InferenceRequest, resolved: ResolvedModel, mode: DecodingMode,
) -> InferenceResult[Any]:
    """Invoke a resolved backend for a resolved decoding mode.

    This is the provider-facing dispatch: it constructs the model, builds
    prompt-cache-aware messages, applies the decoding mode, invokes (with the
    schema-validation retry when constrained), and funnels every raw response
    through budget.track once. Tier resolution, mode resolution, and backend
    selection happen in ainvoke_request (#63); behaviour here is unchanged.
    """
    # Served-engine native guided decoding (SGLang / vLLM): the engine enforces
    # a JSON schema or EBNF grammar at decode time. Dispatched separately from
    # the frontier tool_use / json_schema_strict path below.
    if mode in (DecodingMode.GUIDED_JSON, DecodingMode.GUIDED_GRAMMAR):
        return await _invoke_guided(req, resolved, mode)

    llm = create_chat_model(
        resolved.llm_config,
        model=resolved.model,
        temperature=req.sampling.temperature,
        max_tokens=req.sampling.max_tokens,
    )
    for t in req.tools:
        llm = llm.bind_tools([t.tool])

    # REASON_THEN_EXTRACT: unconstrained reasoning, then constrained extraction.
    if req.extraction_policy == ExtractionPolicy.REASON_THEN_EXTRACT and req.output_schema:
        reason_msgs = _build_messages(req, resolved)
        reasoning = await llm.ainvoke(reason_msgs)
        _track(req, reasoning)
        extract_msgs = list(reason_msgs)
        extract_msgs.append(reasoning)
        extract_msgs.append(HumanMessage(content=(
            "Now extract the structured result from your reasoning above, "
            f"conforming exactly to the {getattr(req.output_schema, '__name__', 'schema')}."
        )))
        parsed, raw, err, attempts = await _invoke_structured(
            llm, req.output_schema, extract_msgs, req, mode=mode,
        )
        # usage covers BOTH calls (budget.track already saw each); the returned
        # field must not undercount the reasoning tokens.
        ru, eu = _usage_of(reasoning), _usage_of(raw)
        return InferenceResult(
            parsed=parsed, raw_message=raw,
            text=getattr(reasoning, "content", None),
            tool_calls=getattr(raw, "tool_calls", []) or [],
            usage=UsageDelta(ru.input_tokens + eu.input_tokens,
                             ru.output_tokens + eu.output_tokens),
            resolved=replace(resolved, decoding_mode=mode), attempts=attempts + 1,
            parsing_error=err,
        )

    messages = _build_messages(req, resolved)

    # Unconstrained.
    if mode == DecodingMode.NONE or req.output_schema is None:
        raw = await llm.ainvoke(messages)
        _track(req, raw)
        return InferenceResult(
            parsed=None, raw_message=raw,
            text=(raw.content if isinstance(getattr(raw, "content", None), str) else None),
            tool_calls=getattr(raw, "tool_calls", []) or [],
            usage=_usage_of(raw),
            resolved=replace(resolved, decoding_mode=mode), attempts=1,
        )

    # Constrained decoding (tool_use / json_schema_strict / json_object) — all go
    # through with_structured_output; json_object uses method="json_mode" + a
    # schema hint (see _invoke_structured). The resolved mode is recorded on
    # ``resolved`` for observability; guided served-engine modes are rejected
    # above until #13 wires their shaping.
    parsed, raw, err, attempts = await _invoke_structured(
        llm, req.output_schema, messages, req, mode=mode,
    )
    return InferenceResult(
        parsed=parsed, raw_message=raw,
        text=None, tool_calls=getattr(raw, "tool_calls", []) or [],
        usage=_usage_of(raw),
        resolved=replace(resolved, decoding_mode=mode),
        attempts=attempts,
    )


# ------------------------------------------------ backend resolution + drivers


def _base_url_of(resolved: ResolvedModel) -> str:
    cfg = resolved.llm_config
    if resolved.provider == "anthropic":
        return cfg.anthropic_base_url or ""
    return cfg.openai_base_url or ""


def delivery_profile_for(resolved: ResolvedModel) -> DeliveryProfile:
    """Classify a resolved tier into a DeliveryProfile (#63).

    Frontier is warm/managed/per-token. Served engines (vLLM/SGLang) and generic
    OpenAI-compatible endpoints are self-deployed; the concrete backend (Modal,
    RunPod pod, Ollama) is inferred from the base URL so the cost and scheduling
    layers can reason about readiness and billing. The RunPod async-job backend
    is declared here but only driven in #64.
    """
    base = _base_url_of(resolved)
    engine = resolved.engine
    # Guided decoding (response_format JSON schema / EBNF) is only wired for vLLM
    # and SGLang; a generic OpenAI-compatible gateway must not advertise it.
    native_guided = engine in (ProviderEngine.VLLM, ProviderEngine.SGLANG)

    # Parse the URL rather than substring-match it: substrings would let
    # api.openai.com.evil read as the real frontier, or a remote :11434 read as
    # local Ollama. A custom host means the request is not going to the canonical
    # provider API, which is how a self-hosted endpoint is reached even with the
    # default FRONTIER engine (provider=openai + OPENAI_BASE_URL, the #4 seam).
    host = ""
    if base:
        parts = urlsplit(base if "://" in base else f"http://{base}")
        host = (parts.hostname or "").lower()
    is_provider_host = host in ("api.openai.com", "api.anthropic.com")
    has_custom_base = bool(host) and not is_provider_host

    if host.endswith("proxy.runpod.net"):
        kind = BackendKind.RUNPOD_POD          # always-on pod behind the proxy
    elif host == "api.runpod.ai":
        kind = BackendKind.RUNPOD_JOB          # serverless async-job endpoint (#64)
    elif host.endswith(".modal.run"):
        kind = BackendKind.MODAL
    elif host in ("localhost", "127.0.0.1") or "ollama" in host:
        kind = BackendKind.OLLAMA
    elif (engine in (ProviderEngine.VLLM, ProviderEngine.SGLANG,
                     ProviderEngine.OPENAI_COMPATIBLE) or has_custom_base):
        kind = BackendKind.OPENAI_COMPAT
    else:
        kind = BackendKind.FRONTIER

    if kind == BackendKind.FRONTIER:
        readiness, lifecycle, scaling, billing, batching = (
            "warm", "managed", "provider", "per_token", "provider_managed")
    elif kind == BackendKind.OLLAMA:
        readiness, lifecycle, scaling, billing, batching = (
            "local_load", "local", "single_instance", "free", "continuous_worker")
    elif kind == BackendKind.MODAL:
        readiness, lifecycle, scaling, billing, batching = (
            "scale_to_zero", "self_deployed", "scale_to_zero", "per_gpu_second",
            "continuous_worker")
    else:  # RUNPOD_POD / RUNPOD_JOB / OPENAI_COMPAT
        readiness = "warm" if kind == BackendKind.RUNPOD_POD else "scale_to_zero"
        lifecycle = "self_deployed"
        scaling = ("single_instance" if kind == BackendKind.RUNPOD_POD
                   else "scale_to_zero")
        billing = ("per_gpu_second"
                   if kind in (BackendKind.RUNPOD_POD, BackendKind.RUNPOD_JOB)
                   else "per_token")
        batching = "continuous_worker"

    caps = CapabilitySet(
        tools=True,
        strict_json=True,
        guided_json=native_guided,
        grammar=native_guided,
        streaming=True,
        prompt_cache=(resolved.provider == "anthropic"),
        batch_api=(kind == BackendKind.FRONTIER),
    )
    return DeliveryProfile(
        backend_id=f"{kind.value}:{resolved.model}",
        kind=kind, invocation="sync_chat", readiness=readiness,
        lifecycle=lifecycle, scaling=scaling, billing=billing,
        batching=batching, capabilities=caps,
    )


def resolve_backend(
    cfg: LLMConfig, tier: InferenceTier, *, model_override: str | None = None,
) -> ResolvedBackend:
    """resolve_tier plus a DeliveryProfile (#63). The uniform envelope stays
    InferenceRequest; the profile carries the per-backend semantics."""
    resolved = resolve_tier(cfg, tier, model_override=model_override)
    return ResolvedBackend(resolved=resolved, profile=delivery_profile_for(resolved))


class InferenceBackend(Protocol):
    profile: DeliveryProfile

    async def invoke(
        self, req: InferenceRequest, resolved: ResolvedModel, mode: DecodingMode,
    ) -> InferenceResult[Any]: ...


class SyncChatBackend:
    """Synchronous request/response driver over the OpenAI/Anthropic wire:
    frontier, generic OpenAI-compatible, Modal served endpoints, RunPod pods, and
    Ollama. All delegate to the shared _dispatch, so behaviour is identical to the
    pre-#63 path; the driver carries the profile and gives #64's async-job driver
    a seam to slot beside."""

    def __init__(self, profile: DeliveryProfile) -> None:
        self.profile = profile

    async def invoke(
        self, req: InferenceRequest, resolved: ResolvedModel, mode: DecodingMode,
    ) -> InferenceResult[Any]:
        return await _dispatch(req, resolved, mode)


def select_backend(rb: ResolvedBackend) -> InferenceBackend:
    """Pick the driver for a resolved backend. Every kind uses SyncChatBackend
    today; #64 registers an async-job driver for kind == RUNPOD_JOB."""
    return SyncChatBackend(rb.profile)


def _usage_record(result: InferenceResult, profile: DeliveryProfile) -> UsageRecord:
    return UsageRecord(
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        backend_id=profile.backend_id,
        backend_kind=profile.kind.value,
    )


# Signatures a scale-to-zero endpoint emits while cold-starting / with no ready
# workers. Matched ONLY for scale_to_zero backends (see ainvoke_request), so a
# 404 on a warm frontier is never mistaken for a cold start. Deliberately
# narrow: a permanent bad base_url/model 404 does NOT carry these bodies, and
# even if a raw 404 slips through, treating a warm-backend 404 as terminal is
# the safe default (only scale_to_zero opts in).
# Status codes that, on a scale_to_zero backend, indicate a warming endpoint on
# their own. 404 and 429 are DELIBERATELY excluded: a bare 404 ("model does not
# exist") or 429 ("rate limit exceeded") is usually a permanent/rate error, and
# masking it as transient would burn the retry cap and mislabel the failure. A
# genuine cold 404/429 still matches via a _COLD_START_MARKERS body signal
# (e.g. "no workers", "endpoint is starting"). RunPod's real cold-start signal
# is a client timeout/hold, caught by the markers, not a bare status.
_COLD_START_STATUSES = {408, 425, 500, 502, 503, 504}
_COLD_START_MARKERS = (
    "no workers", "no ready workers", "worker is starting", "initializing",
    "not ready", "cold start", "endpoint is starting", "please try again",
    "temporarily unavailable", "502 bad gateway", "503 service unavailable",
    "connection refused", "connection reset", "timed out", "timeout",
)


def _is_cold_start_error(e: BaseException) -> bool:
    """True if an exception looks like a scale-to-zero endpoint that is warming
    rather than a permanent misconfiguration. Status-code OR body-marker match;
    caller gates this on readiness == scale_to_zero.

    This is the FALLBACK heuristic. For RunPod serverless the primary signal is
    ``_runpod_health_verdict``, which asks the gateway for worker counts instead
    of inferring state from error prose. Guessing from a status code is the
    wrong tool for this decision because the two mistakes are not symmetric:
    calling a permanent error transient burns a bounded retry budget, while
    calling a transient error permanent silently drops a security alert
    (issue #77)."""
    status = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if status in _COLD_START_STATUSES:
        return True
    msg = str(e).lower()
    return any(m in msg for m in _COLD_START_MARKERS)


def _runpod_endpoint_id(base_url: str) -> str | None:
    """The serverless endpoint id in an api.runpod.ai base URL, or None.

    Matches ``https://api.runpod.ai/v2/<endpoint_id>/...`` and nothing else, so
    a pod behind proxy.runpod.net or any other OpenAI-compatible host never
    triggers a health probe it has no endpoint for."""
    if not base_url:
        return None
    parts = urlsplit(base_url if "://" in base_url else f"https://{base_url}")
    if (parts.hostname or "").lower() != "api.runpod.ai":
        return None
    segments = [s for s in (parts.path or "").split("/") if s]
    if len(segments) >= 2 and segments[0] == "v2":
        return segments[1]
    return None


async def _runpod_health_verdict(resolved: ResolvedModel) -> bool | None:
    """Ask RunPod's /health whether the endpoint can serve right now.

    Three-way, and each answer has a distinct meaning:

    False — a worker is ready or idle. The gateway had capacity, so the error
    in hand is real: a 404 alongside ready workers is a wrong model or path,
    not a cold start. Fail fast rather than burning the retry budget.

    True — no capacity, but visible warm-up activity: a worker initializing or
    running, or jobs queued. Transient whatever the status code said.
    ``running`` deliberately does NOT count as capacity: measured live during
    a real cold start, the booting worker reports ``running=1`` with
    ``ready=0`` while the queue backs up — the first version of this predicate
    counted it, judged the endpoint able to serve, and turned a mid-warm-up
    timeout terminal, which is precisely the mistake this probe exists to
    prevent. (A warm endpoint saturated to running-only is released too, and
    retrying against a busy endpoint is the right call anyway.)

    None — no capacity and no activity (paused, or a lone unhealthy worker
    going nowhere), or the probe itself failed. The counts cannot distinguish
    an endpoint that will recover from one that will not, so the caller's
    prose heuristic decides, exactly as it did before this probe existed.

    This exists because RunPod's error vocabulary is not a contract: measured
    live, a missing endpoint answers 404 "endpoint not found" and a paused one
    answers a bare 500, and neither body carries the marker strings the
    fallback heuristic greps for. Worker counts are the authoritative signal
    and cost one 3-second call on a path that is already failed."""
    endpoint_id = _runpod_endpoint_id(_base_url_of(resolved))
    if not endpoint_id:
        return None
    api_key = resolved.llm_config.openai_api_key
    if not api_key:
        return None
    # Configurable because the probe sits on a failing path: a churning
    # immediate-500 endpoint pays it on every attempt, and 4 attempts x 3s is
    # real throughput lost before the run terminalises. Still bounded well
    # under the worker's 60s lease TTL either way.
    timeout = float(os.environ.get("RUNPOD_HEALTH_TIMEOUT_SECONDS", "2.0"))
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/health",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                # Loud, because a silently dead probe permanently disables the
                # real fix: a health credential that stopped working would
                # otherwise demote every decision to the prose heuristic and
                # nobody would know. Status only — never the key.
                logger.warning(
                    "runpod_health_probe_unusable endpoint=%s status=%s",
                    endpoint_id, resp.status_code,
                )
                return None
            body = resp.json()
    except Exception as probe_err:  # noqa: BLE001 — a failed probe must never mask the real error
        logger.warning(
            "runpod_health_probe_failed endpoint=%s error=%s",
            endpoint_id, type(probe_err).__name__,
        )
        return None
    workers = body.get("workers", {})
    jobs = body.get("jobs", {})
    if int(workers.get("ready", 0)) + int(workers.get("idle", 0)) > 0:
        # Capacity exists: the gateway could have served, so the error in hand
        # is real. A 404 alongside ready workers is a wrong model or path.
        return False
    activity = (
        int(workers.get("initializing", 0))
        + int(workers.get("running", 0))
        + int(jobs.get("inQueue", 0))
        + int(jobs.get("inProgress", 0))
    )
    if activity > 0:
        # No capacity but visible warm-up: a worker booting or work queued.
        # Transient, whatever the status code said.
        return True
    # No capacity and no activity: paused, throttled into silence, or a lone
    # unhealthy worker going nowhere. The counts cannot distinguish an
    # endpoint that will recover from one that will not, so decline to rule
    # and let the caller's heuristic decide — a paused endpoint's 500s stay
    # transient there, and a dead configuration's bare 404 stays terminal.
    return None


async def ainvoke_request(
    req: InferenceRequest, *, cfg: LLMConfig,
) -> InferenceResult[Any]:
    """Execute an InferenceRequest through the single seam (#63).

    Resolves the tier to a backend (provider/model/engine/decoding plus a
    DeliveryProfile), resolves the decoding mode, selects the backend driver,
    invokes, and tags the result with a backend-aware UsageRecord. The frontier
    and served paths are unchanged; the abstraction adds the profile, the driver
    seam, and the usage record.
    """
    rb = resolve_backend(cfg, req.tier, model_override=req.model_override)
    resolved = rb.resolved
    # An explicit per-request mode wins; otherwise fall back to the tier's
    # configured default_decoding_mode (carried on resolved) before AUTO.
    requested_mode = req.decoding_mode
    if requested_mode == DecodingMode.AUTO and resolved.decoding_mode != DecodingMode.AUTO:
        requested_mode = resolved.decoding_mode
    mode = resolve_decoding_mode(
        requested_mode, engine=resolved.engine, provider=resolved.provider,
        has_schema=req.output_schema is not None, has_grammar=req.grammar is not None,
    )
    backend = select_backend(rb)
    try:
        result = await backend.invoke(req, resolved, mode)
    except Exception as e:  # noqa: BLE001 — re-raise as-is unless it's a
        # profile-scoped cold-start signal, in which case reclassify it so the
        # worker can release-and-retry instead of failing the run terminally.
        if rb.profile.readiness == "scale_to_zero":
            # RunPod serverless: ask the gateway for worker counts rather than
            # inferring warm/cold from the error's status code or prose. A
            # definite answer in either direction wins over the heuristic; only
            # a failed probe falls through to it (#77).
            if rb.profile.kind == BackendKind.RUNPOD_JOB:
                warming = await _runpod_health_verdict(resolved)
                if warming is True:
                    raise ServerlessUnavailableError(str(e)[:500]) from e
                if warming is False:
                    raise
            if _is_cold_start_error(e):
                raise ServerlessUnavailableError(str(e)[:500]) from e
        raise
    result.usage_record = _usage_record(result, rb.profile)
    return result


__all__ = [
    "InferenceTier", "ProviderEngine", "DecodingMode", "ExtractionPolicy",
    "SamplingParams", "ToolSpec", "InferenceAccounting", "InferenceRequest",
    "ResolvedModel", "UsageDelta", "InferenceResult",
    "BackendKind", "CapabilitySet", "DeliveryProfile", "UsageRecord",
    "ResolvedBackend", "InferenceBackend", "SyncChatBackend",
    "resolve_tier", "resolve_tier_sampling", "resolve_decoding_mode",
    "resolve_backend", "delivery_profile_for", "select_backend",
    "guided_request_kwargs", "ainvoke_request",
]
