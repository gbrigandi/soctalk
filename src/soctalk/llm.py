"""LLM provider factory.

SocTalk supports either:
- Anthropic (via langchain-anthropic)
- OpenAI-compatible (via langchain-openai)

The provider selection is mutually exclusive and configured via environment.
See `soctalk.config.LLMConfig`.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from soctalk.config import LLMConfig


class LLMProviderError(ValueError):
    """Raised when the configured LLM provider is invalid or incomplete."""


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
            "temperature": temperature,
            "max_tokens": max_tokens,
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
                temperature=temperature,
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
            "temperature": temperature,
            "max_tokens": max_tokens,
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
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )

    raise LLMProviderError(
        f"Unsupported LLM provider: {llm_config.provider!r}. Expected 'anthropic' or 'openai'."
    )
