"""
Global exception handlers for FastAPI.

Equivalent to CAgent's @RestControllerAdvice GlobalExceptionHandler.

Translates domain exceptions (MiniblogException hierarchy) and standard
FastAPI/Starlette exceptions into a uniform ApiResponse envelope.

Wire into the app in main.py:
    from core.exception_handler import register_exception_handlers
    register_exception_handlers(app)
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.exceptions import (
    MiniblogException,
)
from core.response import ApiResponse

# Lazy import to avoid circular dependency during startup
_logger = None


def _get_logger():
    global _logger
    if _logger is None:
        try:
            from loguru import logger

            _logger = logger
        except ImportError:
            import logging

            _logger = logging.getLogger(__name__)
    return _logger


# ── HTTP status mapping ────────────────────────────────────────────────────────


def _domain_code_to_http_status(code: int) -> int:
    """Map application error code to HTTP status code."""
    if 400 <= code <= 599:
        return code
    # Fallback mapping for non-standard codes
    return 500


# ── Exception Handlers ────────────────────────────────────────────────────────


async def miniblog_exception_handler(
    request: Request,
    exc: MiniblogException,
) -> JSONResponse:
    """
    Handle all MiniblogException subclasses.
    Logs 5xx errors with full context; 4xx are logged at WARNING level.
    """
    logger = _get_logger()
    http_status = _domain_code_to_http_status(exc.code)

    if http_status >= 500:
        logger.error(
            "Server error: {message} | path={path} | code={code}",
            message=exc.message,
            path=request.url.path,
            code=exc.code,
        )
    else:
        logger.warning(
            "Client error: {message} | path={path} | code={code}",
            message=exc.message,
            path=request.url.path,
            code=exc.code,
        )

    return ApiResponse.error(
        code=exc.code,
        message=exc.message,
        data=exc.data,
    ).to_json_response(http_status)


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """
    Wrap Starlette/FastAPI HTTPException into ApiResponse envelope.
    Preserves backward compatibility for existing raise HTTPException(...) calls.
    """
    logger = _get_logger()
    if exc.status_code >= 500:
        logger.error(
            "HTTP error {status}: {detail} | path={path}",
            status=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
        )

    return ApiResponse.error(
        code=exc.status_code,
        message=str(exc.detail) if exc.detail else _default_message(exc.status_code),
    ).to_json_response(exc.status_code)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """
    Handle Pydantic v2 request validation errors (422 Unprocessable Entity).
    Returns structured field errors in the `data` field.
    """
    field_errors = []
    for error in exc.errors():
        field_errors.append(
            {
                "field": " → ".join(str(loc) for loc in error["loc"] if loc != "body"),
                "message": error["msg"],
                "type": error["type"],
            }
        )

    return ApiResponse.error(
        code=422,
        message="Request validation failed",
        data={"errors": field_errors},
    ).to_json_response(status.HTTP_422_UNPROCESSABLE_ENTITY)


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Catch-all handler for unhandled exceptions.
    Logs full traceback internally; NEVER leaks implementation details to caller.
    """
    logger = _get_logger()
    logger.exception(
        "Unhandled exception on {method} {path}",
        method=request.method,
        path=request.url.path,
    )

    return ApiResponse.error(
        code=500,
        message="An internal server error occurred",
    ).to_json_response(status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Registration helper ────────────────────────────────────────────────────────


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all global exception handlers on the FastAPI app instance.
    Call this in main.py before the app starts.
    """
    app.add_exception_handler(MiniblogException, miniblog_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)  # type: ignore[arg-type]


# ── Private helpers ────────────────────────────────────────────────────────────


def _default_message(status_code: int) -> str:
    messages = {
        400: "Bad request",
        401: "Authentication required",
        403: "Access denied",
        404: "Resource not found",
        405: "Method not allowed",
        409: "Conflict",
        422: "Validation error",
        429: "Too many requests",
        500: "Internal server error",
        502: "Upstream service error",
        503: "Service unavailable",
    }
    return messages.get(status_code, "Error")
