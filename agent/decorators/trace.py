"""
Lightweight distributed tracing decorators.

Equivalent to CAgent's @RagTraceRoot / @RagTraceNode AOP annotations.

Design:
  @trace_root — marks the entry point of a trace tree (e.g. a Celery task).
                Generates a new trace_id and stores it in ContextVar.
  @trace_node — marks a sub-operation within an active trace.
                Records name, duration, and logs with parent trace_id.

Logs are written via loguru and include trace_id for correlation.
The trace_id is also propagated to the logging context (core.logging),
so ALL log lines within a traced call automatically carry the trace_id.

Usage:
    from decorators.trace import trace_root, trace_node
    from loguru import logger

    @trace_root(name="podcast_generation")
    async def generate_podcast(session_id: str):
        script = await write_script(session_id)
        audio = await generate_audio(script)
        return audio

    @trace_node(name="write_script")
    async def write_script(session_id: str) -> str:
        ...

    @trace_node(name="generate_audio")
    async def generate_audio(script: str) -> bytes:
        ...
"""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from typing import Callable, Optional

from loguru import logger

from core.logging import get_trace_id, set_trace_id


def trace_root(name: Optional[str] = None):
    """
    Mark a function as the root of a trace tree.

    Generates a new trace_id, stores it in ContextVar, and logs
    TRACE_START / TRACE_END with total elapsed time.

    Equivalent to CAgent's @RagTraceRoot annotation.
    """
    def decorator(func: Callable) -> Callable:
        _name = name or func.__qualname__

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                trace_id = str(uuid.uuid4())
                set_trace_id(trace_id)
                start = time.perf_counter()
                logger.info(
                    "TRACE_START [{name}] trace_id={trace_id}",
                    name=_name,
                    trace_id=trace_id,
                )
                try:
                    result = await func(*args, **kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        "TRACE_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms status=ok",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                    )
                    return result
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.error(
                        "TRACE_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms "
                        "status=error error={error}",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                        error=str(e),
                    )
                    raise
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                trace_id = str(uuid.uuid4())
                set_trace_id(trace_id)
                start = time.perf_counter()
                logger.info(
                    "TRACE_START [{name}] trace_id={trace_id}",
                    name=_name,
                    trace_id=trace_id,
                )
                try:
                    result = func(*args, **kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        "TRACE_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms status=ok",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                    )
                    return result
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.error(
                        "TRACE_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms "
                        "status=error error={error}",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                        error=str(e),
                    )
                    raise
            return sync_wrapper

    return decorator


def trace_node(name: Optional[str] = None):
    """
    Mark a function as a sub-operation within an active trace.

    Logs SPAN_START / SPAN_END with duration. Uses the current trace_id
    from ContextVar (set by the enclosing @trace_root).

    Equivalent to CAgent's @RagTraceNode annotation.
    """
    def decorator(func: Callable) -> Callable:
        _name = name or func.__qualname__

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                trace_id = get_trace_id() or "no-trace"
                span_id = str(uuid.uuid4())[:8]
                start = time.perf_counter()
                logger.debug(
                    "SPAN_START [{name}] trace_id={trace_id} span_id={span_id}",
                    name=_name,
                    trace_id=trace_id,
                    span_id=span_id,
                )
                try:
                    result = await func(*args, **kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.debug(
                        "SPAN_END [{name}] trace_id={trace_id} span_id={span_id} "
                        "elapsed={elapsed:.1f}ms status=ok",
                        name=_name,
                        trace_id=trace_id,
                        span_id=span_id,
                        elapsed=elapsed_ms,
                    )
                    return result
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.warning(
                        "SPAN_END [{name}] trace_id={trace_id} span_id={span_id} "
                        "elapsed={elapsed:.1f}ms status=error error={error}",
                        name=_name,
                        trace_id=trace_id,
                        span_id=span_id,
                        elapsed=elapsed_ms,
                        error=str(e),
                    )
                    raise
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                trace_id = get_trace_id() or "no-trace"
                span_id = str(uuid.uuid4())[:8]
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.debug(
                        "SPAN_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms status=ok",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                    )
                    return result
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.warning(
                        "SPAN_END [{name}] trace_id={trace_id} elapsed={elapsed:.1f}ms "
                        "status=error error={error}",
                        name=_name,
                        trace_id=trace_id,
                        elapsed=elapsed_ms,
                        error=str(e),
                    )
                    raise
            return sync_wrapper

    return decorator
