"""LLM provider factory.

SocTalk supports either:
- Anthropic (via langchain-anthropic)
- OpenAI-compatible (via langchain-openai)

The provider selection is mutually exclusive and configured via environment.
See `soctalk.config.LLMConfig`.
"""

from __future__ import annotations

import os
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from soctalk.config import LLMConfig


class ServerlessUnavailableError(ValueError):
    """A scale-to-zero backend was unavailable in a way that is TRANSIENT.

    Raised ONLY when the resolved DeliveryProfile is scale_to_zero, so the same
    error on a warm frontier stays a permanent ``provider_error``. The worker
    releases a run that fails this way for a bounded retry on the same run_id
    (issue #77).

    How transience is decided, in order:
      1. RunPod serverless (api.runpod.ai): the gateway's /health counts. A
         ready or idle worker means capacity, so the error is real; no
         capacity plus warm-up activity (initializing/running workers, queued
         jobs) means warming, whatever the status code said; no capacity and
         no activity is inconclusive and falls through to 2. Measured live:
         RunPod answers 404 only for a MISSING endpoint ("endpoint not
         found"), holds cold-start requests until the client times out, and
         answers a bare 500 when paused.
      2. Fallback for other scale-to-zero endpoints, or when the probe fails:
         status in {408, 425, 500, 502, 503, 504}, or a cold-start marker
         substring in the error body (see _COLD_START_MARKERS in inference.py).
         A bare 404/429 with no marker is deliberately NOT matched — the exact
         phrasings matched are the ones in that tuple, no more."""


class LLMProviderError(ValueError):
    """Raised when the configured LLM provider is invalid or incomplete."""


class SchemaValidationError(ValueError):
    """The model's response failed structured-output schema validation
    after the retry budget was exhausted."""


def classify_llm_error(e: BaseException) -> str:
    """Bucket an LLM-provider exception into a stable category string.

    Categories the worker actually branches on:
      * ``serverless_unavailable`` — scale-to-zero backend cold-starting; the
                                     worker releases the run for a bounded retry
                                     rather than failing it terminally (#77)
      * ``insufficient_credit`` — provider billing / quota lack
      * ``rate_limited``        — provider 429 / TPM RPM exceeded
      * ``provider_error``      — other 4xx/5xx from the provider
      * ``timeout``             — local/transport timeout
      * ``schema_validation``   — structured output failed validation
      * ``unknown``             — fallback

    The category goes to logs + state["verdict_error"] /
    state["supervisor_error"]; the raw error string is intentionally
    kept out of any user-facing field.
    """
    if isinstance(e, ServerlessUnavailableError):
        return "serverless_unavailable"
    if isinstance(e, SchemaValidationError):
        return "schema_validation"
    msg = str(e).lower()
    status = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if "credit balance" in msg or "insufficient_quota" in msg or "billing" in msg:
        return "insufficient_credit"
    if status == 429 or "rate limit" in msg or "tokens per minute" in msg:
        return "rate_limited"
    if status and 400 <= int(status) < 600:
        return "provider_error"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "unknown"


def make_system_message(text: str, llm_config: LLMConfig) -> Any:
    """Build the system message, opting into provider prompt caching.

    Anthropic caching is opt-in per content block: the block-form system
    message with ``cache_control: ephemeral`` caches the tools+system
    prefix (tool definitions precede system in Anthropic's prefix order,
    so structured-output schemas ride along). OpenAI-compatible providers
    cache stable prefixes automatically — plain string content there.
    """
    from langchain_core.messages import SystemMessage

    if llm_config.provider == "anthropic":
        return SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
    return SystemMessage(content=text)


async def ainvoke_structured(
    llm: BaseChatModel,
    schema: type,
    messages: list[Any],
    *,
    on_response: Any = None,
) -> Any:
    """Invoke ``llm`` with schema-enforced output and one validation retry.

    Uses ``with_structured_output(include_raw=True)`` so the raw AIMessage
    (with ``usage_metadata``) survives for budget tracking — ``on_response``
    is called with each raw message. On a validation failure the error is
    fed back to the model once (self-correction); a second failure raises
    :class:`SchemaValidationError`, which ``classify_llm_error`` maps to
    ``schema_validation`` so the run fails loudly instead of a fabricated
    default steering triage.

    Provider errors (rate limit, timeout, 5xx) raise as usual — retry for
    those lives in the SDK layer configured by ``create_chat_model``.
    """
    from langchain_core.messages import HumanMessage

    structured = llm.with_structured_output(schema, include_raw=True)

    result = await structured.ainvoke(messages)
    raw = result.get("raw")
    if raw is not None and on_response is not None:
        on_response(raw)
    if result.get("parsed") is not None:
        return result["parsed"]

    parsing_error = result.get("parsing_error")
    feedback = (
        f"Your previous response failed validation against the "
        f"{getattr(schema, '__name__', 'output')} schema: {parsing_error}. "
        "Respond again, following the schema exactly."
    )
    retry_messages = list(messages)
    if raw is not None:
        retry_messages.append(raw)
        # invalid_tool_calls covers truncated/malformed calls (e.g. max_tokens cut a
        # tool call mid-JSON) — those still put a tool_use block in the turn and
        # still demand a tool_result.
        tool_calls = (getattr(raw, "tool_calls", None) or []) + (
            getattr(raw, "invalid_tool_calls", None) or []
        )
        if tool_calls:
            # Structured output rides on tool calling: an assistant turn carrying
            # tool_use blocks MUST be followed by matching tool_result blocks
            # (newer Claude models reject the request otherwise). Deliver the
            # validation feedback as the tool results.
            from langchain_core.messages import ToolMessage

            retry_messages.extend(
                ToolMessage(content=feedback, tool_call_id=tc["id"]) for tc in tool_calls
            )
        else:
            retry_messages.append(HumanMessage(content=feedback))
    else:
        retry_messages.append(HumanMessage(content=feedback))
    result = await structured.ainvoke(retry_messages)
    raw = result.get("raw")
    if raw is not None and on_response is not None:
        on_response(raw)
    if result.get("parsed") is not None:
        return result["parsed"]

    raise SchemaValidationError(str(result.get("parsing_error")))


# Newer Claude models (Opus 4.7/4.8, Sonnet 5, Fable/Mythos 5) removed the `temperature`
# sampling parameter — requests carrying it are rejected with a 400 ("`temperature` is
# deprecated for this model"). The correct client behavior is to omit it entirely for
# those models; older models (Sonnet 4.x, Opus 4.6 and earlier, Haiku) still accept it.
_NO_TEMPERATURE_MODELS = re.compile(r"opus-4-[789]|sonnet-5|fable|mythos")


def _sampling_kwargs(model: str, temperature: float) -> dict[str, Any]:
    if _NO_TEMPERATURE_MODELS.search(model):
        return {}
    return {"temperature": temperature}


def create_chat_model(
    llm_config: LLMConfig,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    **kwargs: Any,
) -> BaseChatModel:
    """Create a chat model for the configured provider.

    Args:
        llm_config: LLM configuration.
        model: Model name (provider-specific).
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate.
        kwargs: Provider-specific keyword args (reserved for future use).

    Returns:
        A LangChain chat model instance.
    """
    if llm_config.anthropic_api_key and llm_config.openai_api_key:
        raise LLMProviderError(
            "Both ANTHROPIC_API_KEY and OPENAI_API_KEY are set. Choose exactly one LLM provider."
        )

    if llm_config.provider == "anthropic":
        if not llm_config.anthropic_api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY is required when SOCTALK_LLM_PROVIDER=anthropic")

        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as e:
            raise LLMProviderError(
                "Anthropic provider selected but `langchain-anthropic` is not installed."
            ) from e

        anthropic_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": llm_config.anthropic_api_key,
            **_sampling_kwargs(model, temperature),
            "max_tokens": max_tokens,
            # Bounded transport behavior: the SDK default timeout is 600s
            # and it already retries 408/429/5xx with backoff — we bound
            # the timeout and keep retries at the SDK layer (single layer,
            # no app-side retry wrapping).
            "timeout": llm_config.timeout_seconds,
            "max_retries": llm_config.max_retries,
            **kwargs,
        }
        if llm_config.anthropic_base_url:
            anthropic_kwargs["base_url"] = llm_config.anthropic_base_url

        try:
            return ChatAnthropic(**anthropic_kwargs)
        except TypeError:
            if llm_config.anthropic_base_url and not os.getenv("ANTHROPIC_BASE_URL"):
                os.environ["ANTHROPIC_BASE_URL"] = llm_config.anthropic_base_url
            return ChatAnthropic(
                model=model,
                api_key=llm_config.anthropic_api_key,
                **_sampling_kwargs(model, temperature),
                max_tokens=max_tokens,
                **kwargs,
            )

    if llm_config.provider == "openai":
        if not llm_config.openai_api_key:
            raise LLMProviderError("OPENAI_API_KEY is required when SOCTALK_LLM_PROVIDER=openai")

        # Prefer environment-driven configuration for OpenAI-compatible providers.
        # `langchain-openai`/`openai` pick up OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_ORGANIZATION.
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise LLMProviderError(
                "OpenAI provider selected but `langchain-openai` is not installed."
            ) from e

        openai_kwargs: dict[str, Any] = {
            "model": model,
            # Pass the configured key explicitly (mirrors the Anthropic branch)
            # so per-tier scoped api_key overlays (issue #32 resolve_tier) reach
            # the client instead of silently falling back to ambient OPENAI_API_KEY.
            "api_key": llm_config.openai_api_key,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": llm_config.timeout_seconds,
            "max_retries": llm_config.max_retries,
            **kwargs,
        }

        if llm_config.openai_base_url:
            openai_kwargs["base_url"] = llm_config.openai_base_url
        if llm_config.openai_organization:
            openai_kwargs["organization"] = llm_config.openai_organization

        try:
            return ChatOpenAI(**openai_kwargs)
        except TypeError:
            if llm_config.openai_base_url and not os.getenv("OPENAI_BASE_URL"):
                os.environ["OPENAI_BASE_URL"] = llm_config.openai_base_url
            if llm_config.openai_organization and not os.getenv("OPENAI_ORGANIZATION"):
                os.environ["OPENAI_ORGANIZATION"] = llm_config.openai_organization
            return ChatOpenAI(
                model=model,
                api_key=llm_config.openai_api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )

    raise LLMProviderError(
        f"Unsupported LLM provider: {llm_config.provider!r}. Expected 'anthropic' or 'openai'."
    )
