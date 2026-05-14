"""
Three-state circuit breaker for external service calls (LLM APIs, Redis, etc.).

Equivalent to CAgent's ModelHealthStore circuit breaker with states:
  CLOSED   → Normal operation. Requests pass through.
  OPEN     → Too many failures. Requests fail immediately (no upstream call).
  HALF_OPEN → Recovery probe. One request is allowed through to test recovery.

State transitions:
  CLOSED  → OPEN     : after `failure_threshold` consecutive failures
  OPEN    → HALF_OPEN : after `recovery_timeout` seconds
  HALF_OPEN → CLOSED  : if probe request succeeds
  HALF_OPEN → OPEN    : if probe request fails

Usage:
    from decorators.circuit_breaker import CircuitBreaker
    from core.exceptions import RemoteException

    openai_breaker = CircuitBreaker("openai", failure_threshold=5, recovery_timeout=60)

    @openai_breaker
    async def call_openai(prompt: str) -> str:
        ...

    # Or use as context manager:
    async with openai_breaker:
        result = await openai_client.chat(...)

Pre-built breakers for common upstreams:
    from decorators.circuit_breaker import openai_breaker, elevenlabs_breaker
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from enum import Enum
from typing import Callable, Type

from loguru import logger

from core.exceptions import RemoteException


class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Thread-safe three-state circuit breaker.
    Works with both async and sync functions.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: Type[Exception] = Exception,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def _can_attempt(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    # Transition to HALF_OPEN for a single probe
                    self._state = CircuitState.HALF_OPEN
                    logger.info(
                        "Circuit [{name}] OPEN → HALF_OPEN after {elapsed:.1f}s",
                        name=self.name,
                        elapsed=elapsed,
                    )
                    return True
                return False
            if self._state == CircuitState.HALF_OPEN:
                return True
        return False

    def _on_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(
                    "Circuit [{name}] HALF_OPEN → CLOSED (probe succeeded)",
                    name=self.name,
                )
            self._state = CircuitState.CLOSED
            self._failure_count = 0

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.error(
                    "Circuit [{name}] HALF_OPEN → OPEN (probe failed)",
                    name=self.name,
                )
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.error(
                    "Circuit [{name}] CLOSED → OPEN after {count} failures",
                    name=self.name,
                    count=self._failure_count,
                )

    def reset(self) -> None:
        """Manually reset circuit to CLOSED (for testing or admin operations)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("Circuit [{name}] manually reset to CLOSED", name=self.name)

    def __call__(self, func: Callable) -> Callable:
        """Use as decorator: @breaker or @breaker()"""
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                if not self._can_attempt():
                    raise RemoteException(
                        upstream=self.name,
                        message=f"Circuit breaker OPEN for '{self.name}'. Failing fast to protect upstream service.",
                        code=503,
                    )
                try:
                    result = await func(*args, **kwargs)
                    self._on_success()
                    return result
                except RemoteException:
                    self._on_failure()
                    raise
                except self.expected_exception as e:
                    self._on_failure()
                    raise RemoteException(
                        upstream=self.name,
                        message=str(e),
                    ) from e

            return async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                if not self._can_attempt():
                    raise RemoteException(
                        upstream=self.name,
                        message=f"Circuit breaker OPEN for '{self.name}'.",
                        code=503,
                    )
                try:
                    result = func(*args, **kwargs)
                    self._on_success()
                    return result
                except RemoteException:
                    self._on_failure()
                    raise
                except self.expected_exception as e:
                    self._on_failure()
                    raise RemoteException(
                        upstream=self.name,
                        message=str(e),
                    ) from e

            return sync_wrapper

    async def __aenter__(self):
        if not self._can_attempt():
            raise RemoteException(
                upstream=self.name,
                message=f"Circuit breaker OPEN for '{self.name}'.",
                code=503,
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._on_success()
        else:
            self._on_failure()
        return False  # don't suppress the exception

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name!r}, state={self._state.value}, failures={self._failure_count}/{self.failure_threshold})"


# ── Pre-built breakers for common upstreams ────────────────────────────────────
# Import these in your agent/service files instead of creating new instances.

openai_breaker = CircuitBreaker(
    name="openai",
    failure_threshold=5,
    recovery_timeout=60,
)

anthropic_breaker = CircuitBreaker(
    name="anthropic",
    failure_threshold=5,
    recovery_timeout=60,
)

elevenlabs_breaker = CircuitBreaker(
    name="elevenlabs",
    failure_threshold=3,
    recovery_timeout=30,
)

tts_breaker = CircuitBreaker(
    name="tts",
    failure_threshold=3,
    recovery_timeout=30,
)
