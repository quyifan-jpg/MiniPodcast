"""
Request/Response logging middleware with X-Request-ID propagation.

Equivalent to CAgent's Spring interceptor that logs every HTTP request
with timing and injects MDC context for correlation.

Features:
- Generates X-Request-ID if not present in request headers
- Injects request_id into loguru context (ContextVar)
- Logs method, path, status, and elapsed time for every request
- Propagates X-Request-ID header back in response
- Skips verbose logging for health/metrics probes
"""

from __future__ import annotations

import time
import uuid

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logging import set_request_id, set_session_id

# Paths to skip from access logging (reduces noise from health probes)
_SILENT_PATHS = frozenset(
    [
        "/health",
        "/health/detail",
        "/metrics",
        "/favicon.ico",
    ]
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every HTTP request with method, path, status code, and latency.
    Injects X-Request-ID into both the request context and response headers.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Reuse caller-supplied request ID for distributed tracing,
        # or generate a new UUID if absent.
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        set_request_id(request_id)

        # Inject session_id from header if present (e.g. podcast agent chat)
        session_id = request.headers.get("X-Session-ID", "")
        if session_id:
            set_session_id(session_id)

        # Store request_id on request.state so downstream handlers can read it
        request.state.request_id = request_id

        start = time.perf_counter()

        try:
            response: Response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "UNHANDLED {method} {path} — {elapsed:.1f}ms | rid={rid}",
                method=request.method,
                path=request.url.path,
                elapsed=elapsed_ms,
                rid=request_id,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Skip noisy health/metrics logs unless they are errors
        if request.url.path not in _SILENT_PATHS or response.status_code >= 400:
            log_fn = logger.warning if response.status_code >= 400 else logger.info
            log_fn(
                "{method} {path} {status} {elapsed:.1f}ms | rid={rid}",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed=elapsed_ms,
                rid=request_id,
            )

        # Propagate X-Request-ID in the response so clients can correlate
        response.headers["X-Request-ID"] = request_id
        return response
