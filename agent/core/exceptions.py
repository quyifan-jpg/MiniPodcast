"""
Application exception hierarchy.

Equivalent to CAgent's ClientException / ServiceException / RemoteException.

Design principle: exceptions do NOT extend FastAPI's HTTPException.
Services remain HTTP-agnostic; the global exception handler translates
domain exceptions into HTTP responses.

Usage:
    from core.exceptions import ClientException, NotFoundException, ServiceException, RemoteException

    raise NotFoundException("Article")
    raise ClientException("Invalid page number", code=400)
    raise ServiceException("Failed to generate podcast")
    raise RemoteException(upstream="openai", message="API quota exceeded")
"""

from __future__ import annotations

from typing import Any, Optional


class MiniblogException(Exception):
    """Base exception for all application errors."""

    def __init__(
        self,
        message: str,
        code: int = 500,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.code}, message={self.message!r})"


# ── 4xx Client Errors ──────────────────────────────────────────────────────────

class ClientException(MiniblogException):
    """
    4xx — The caller made an invalid request.
    Equivalent to CAgent's ClientException.
    """

    def __init__(
        self,
        message: str = "Bad request",
        code: int = 400,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=code, data=data)


class NotFoundException(ClientException):
    """
    404 — Requested resource does not exist.
    Usage: raise NotFoundException("Article")  →  "Article not found"
    """

    def __init__(self, resource: str = "Resource", data: Optional[Any] = None) -> None:
        super().__init__(message=f"{resource} not found", code=404, data=data)


class UnauthorizedException(ClientException):
    """401 — Authentication required."""

    def __init__(
        self,
        message: str = "Authentication required",
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=401, data=data)


class ForbiddenException(ClientException):
    """403 — Authenticated but not authorized."""

    def __init__(
        self,
        message: str = "Access denied",
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=403, data=data)


class TooManyRequestsException(ClientException):
    """429 — Rate limit exceeded."""

    def __init__(
        self,
        message: str = "Too many requests, please try again later",
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=429, data=data)


class ConflictException(ClientException):
    """409 — Resource conflict (e.g. duplicate submission)."""

    def __init__(
        self,
        message: str = "Request conflict",
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=409, data=data)


# ── 5xx Server Errors ──────────────────────────────────────────────────────────

class ServiceException(MiniblogException):
    """
    5xx — An internal error occurred on our side.
    Equivalent to CAgent's ServiceException.
    """

    def __init__(
        self,
        message: str = "Internal server error",
        code: int = 500,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=message, code=code, data=data)


class RemoteException(ServiceException):
    """
    502 — An upstream dependency (LLM, Redis, MySQL, S3) failed.
    Equivalent to CAgent's RemoteException.

    Usage:
        raise RemoteException(upstream="openai", message="API quota exceeded")
        raise RemoteException(upstream="mysql", message="Connection refused")
    """

    def __init__(
        self,
        upstream: str,
        message: str = "Upstream service unavailable",
        code: int = 502,
        data: Optional[Any] = None,
    ) -> None:
        super().__init__(message=f"[{upstream}] {message}", code=code, data=data)
        self.upstream = upstream
