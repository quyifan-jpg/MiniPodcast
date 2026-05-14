"""
Structured logging setup with context propagation.

Equivalent to CAgent's SLF4J + Logback + TransmittableThreadLocal MDC.

Uses loguru for structured logging and Python's contextvars for
request-scoped context propagation across async boundaries.

Usage:
    from core.logging import setup_logging, set_request_id, set_session_id
    from loguru import logger

    setup_logging(app_env="development")   # call once in lifespan
    set_request_id("abc-123")             # call in middleware
    logger.info("Processing request")     # auto-includes request_id in JSON
"""

from __future__ import annotations

import sys
from contextvars import ContextVar
from typing import Optional

from loguru import logger


# ── Context variables (Python equivalent of TransmittableThreadLocal) ──────────

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_session_id_var: ContextVar[str] = ContextVar("session_id", default="")
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_user_id_var: ContextVar[str] = ContextVar("user_id", default="")


def get_request_id() -> str:
    return _request_id_var.get()


def set_request_id(rid: str) -> None:
    _request_id_var.set(rid)


def get_session_id() -> str:
    return _session_id_var.get()


def set_session_id(sid: str) -> None:
    _session_id_var.set(sid)


def get_trace_id() -> str:
    return _trace_id_var.get()


def set_trace_id(tid: str) -> None:
    _trace_id_var.set(tid)


def get_user_id() -> str:
    return _user_id_var.get()


def set_user_id(uid: str) -> None:
    _user_id_var.set(uid)


# ── Context injection filter ───────────────────────────────────────────────────


def _inject_context(record: dict) -> bool:
    """
    Loguru filter that injects request-scoped context variables into every
    log record's `extra` dict. This is the equivalent of MDC in Java.
    """
    record["extra"]["request_id"] = _request_id_var.get("")
    record["extra"]["session_id"] = _session_id_var.get("")
    record["extra"]["trace_id"] = _trace_id_var.get("")
    record["extra"]["user_id"] = _user_id_var.get("")
    return True


# ── Log format strings ─────────────────────────────────────────────────────────

_DEV_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
    "{extra[request_id]: <36} | "
    "<level>{message}</level>"
)

# In production we serialize to JSON for ELK / CloudWatch / Datadog consumption.
# The serialize=True flag makes loguru emit a JSON object per line.
# Custom fields are included via the `extra` dict populated by _inject_context.


# ── Public setup function ──────────────────────────────────────────────────────


def setup_logging(app_env: str = "production", log_level: Optional[str] = None) -> None:
    """
    Configure loguru for the given environment.

    Call once during app startup (e.g. inside the lifespan context manager).

    Args:
        app_env:   "development" | "staging" | "production"
        log_level: Override log level (e.g. "DEBUG"). Defaults to
                   DEBUG for development, INFO for everything else.
    """
    logger.remove()  # remove loguru's default stderr handler

    level = log_level or ("DEBUG" if app_env == "development" else "INFO")

    if app_env == "development":
        # Human-readable colourised output for local dev
        logger.add(
            sys.stderr,
            level=level,
            format=_DEV_FORMAT,
            colorize=True,
            filter=_inject_context,
            backtrace=True,
            diagnose=True,
        )
    else:
        # JSON structured output for production log aggregators
        logger.add(
            sys.stderr,
            level=level,
            serialize=True,  # emit JSON lines
            filter=_inject_context,
            backtrace=False,  # avoid leaking internals in prod
            diagnose=False,
        )

    logger.info(
        "Logging configured | env={env} | level={level}",
        env=app_env,
        level=level,
    )
