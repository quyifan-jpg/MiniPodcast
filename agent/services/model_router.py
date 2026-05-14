"""
Strategy-pattern model router with circuit-breaker-aware failover.

Design:
  - ModelProvider (ABC)        — the Strategy interface
  - OpenAIProvider             — concrete strategy for OpenAI models
  - AnthropicProvider          — concrete strategy for Anthropic models
  - ModelRouter                — the Context that picks a healthy provider

Usage:
    from services.model_router import router

    # Get a LangChain-compatible ChatModel (auto-selects healthy provider)
    llm = router.get_chat_model()

    # Or get an Agno-compatible model for the main orchestrator
    agno_model = router.get_agno_model()

    # After a call succeeds/fails, the breaker updates automatically
    # via the decorator or context-manager on the provider.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List

from loguru import logger

from decorators.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    openai_breaker,
    anthropic_breaker,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy interface
# ═══════════════════════════════════════════════════════════════════════════════


class ModelProvider(ABC):
    """Abstract strategy: knows how to create a chat model for a specific vendor."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name (for logging)."""

    @property
    @abstractmethod
    def breaker(self) -> CircuitBreaker:
        """Circuit breaker instance for this provider."""

    @abstractmethod
    def create_chat_model(self, **overrides: Any) -> Any:
        """Return a LangChain ChatModel (ChatOpenAI / ChatAnthropic / …)."""

    @abstractmethod
    def create_agno_model(self, **overrides: Any) -> Any:
        """Return an Agno-compatible model wrapper (OpenAIChat / …)."""

    @property
    def is_healthy(self) -> bool:
        return self.breaker.state != CircuitState.OPEN


# ═══════════════════════════════════════════════════════════════════════════════
# Concrete strategies
# ═══════════════════════════════════════════════════════════════════════════════


class OpenAIProvider(ModelProvider):
    def __init__(self, model_id: str = "gpt-4o-mini") -> None:
        self._model_id = model_id

    @property
    def name(self) -> str:
        return f"openai/{self._model_id}"

    @property
    def breaker(self) -> CircuitBreaker:
        return openai_breaker

    def create_chat_model(self, **overrides: Any) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=overrides.pop("model", self._model_id),
            temperature=overrides.pop("temperature", 0),
            api_key=os.getenv("OPENAI_API_KEY"),
            **overrides,
        )

    def create_agno_model(self, **overrides: Any) -> Any:
        from agno.models.openai import OpenAIChat

        return OpenAIChat(
            id=overrides.pop("model", self._model_id),
            api_key=os.getenv("OPENAI_API_KEY"),
            **overrides,
        )


class AnthropicProvider(ModelProvider):
    def __init__(self, model_id: str = "claude-sonnet-4-20250514") -> None:
        self._model_id = model_id

    @property
    def name(self) -> str:
        return f"anthropic/{self._model_id}"

    @property
    def breaker(self) -> CircuitBreaker:
        return anthropic_breaker

    def create_chat_model(self, **overrides: Any) -> Any:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=overrides.pop("model", self._model_id),
            temperature=overrides.pop("temperature", 0),
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            **overrides,
        )

    def create_agno_model(self, **overrides: Any) -> Any:
        from agno.models.anthropic import Claude

        return Claude(
            id=overrides.pop("model", self._model_id),
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            **overrides,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Context — the router
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ModelRouter:
    """
    Picks the first healthy ModelProvider from a priority-ordered candidate list.

    If all providers are OPEN (fully degraded), falls back to the first
    candidate and lets the request through anyway (last resort).
    """

    candidates: List[ModelProvider] = field(default_factory=list)

    def _select(self) -> ModelProvider:
        for provider in self.candidates:
            if provider.is_healthy:
                logger.debug("ModelRouter selected {}", provider.name)
                return provider

        # all breakers OPEN — last resort
        fallback = self.candidates[0]
        logger.warning(
            "All model providers degraded, falling back to {}",
            fallback.name,
        )
        return fallback

    def get_chat_model(self, **overrides: Any) -> Any:
        """Return a LangChain ChatModel from the best available provider."""
        provider = self._select()
        return provider.create_chat_model(**overrides)

    def get_agno_model(self, **overrides: Any) -> Any:
        """Return an Agno model from the best available provider."""
        provider = self._select()
        return provider.create_agno_model(**overrides)

    def get_provider(self) -> ModelProvider:
        """Return the selected provider (for manual breaker interaction)."""
        return self._select()

    @property
    def status(self) -> List[dict]:
        """Health snapshot of all candidates (for monitoring/debugging)."""
        return [
            {
                "name": p.name,
                "state": p.breaker.state.value,
                "failures": p.breaker.failure_count,
                "healthy": p.is_healthy,
            }
            for p in self.candidates
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singleton — import `router` directly
# ═══════════════════════════════════════════════════════════════════════════════

router = ModelRouter(
    candidates=[
        OpenAIProvider("gpt-4o-mini"),
        OpenAIProvider("gpt-4o"),
        AnthropicProvider("claude-sonnet-4-20250514"),
    ]
)
