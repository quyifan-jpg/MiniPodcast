"""
Standardized API response envelope.

Equivalent to CAgent's Result<T> with {code, message, data} structure.

All API endpoints should return ApiResponse to maintain a consistent
contract for API consumers.

Usage:
    from core.response import ApiResponse

    return ApiResponse.ok(data={"id": 1})
    return ApiResponse.error(code=404, message="Article not found")
    return ApiResponse.paginated(items=[...], total=100, page=1, per_page=10)
"""

from __future__ import annotations

import math
from typing import Any, Generic, List, Optional, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """
    Unified response envelope used by all API endpoints.

    HTTP status code is set separately on the JSONResponse;
    `code` here carries the application-level status so clients
    can distinguish success variants and domain error codes.
    """

    code: int = 200
    message: str = "success"
    data: Optional[T] = None

    # ── Factory constructors ───────────────────────────────────────────────

    @classmethod
    def ok(
        cls,
        data: Optional[Any] = None,
        message: str = "success",
    ) -> "ApiResponse":
        return cls(code=200, message=message, data=data)

    @classmethod
    def created(
        cls,
        data: Optional[Any] = None,
        message: str = "Created successfully",
    ) -> "ApiResponse":
        return cls(code=201, message=message, data=data)

    @classmethod
    def error(
        cls,
        code: int,
        message: str,
        data: Optional[Any] = None,
    ) -> "ApiResponse":
        return cls(code=code, message=message, data=data)

    @classmethod
    def paginated(
        cls,
        items: List[Any],
        total: int,
        page: int,
        per_page: int,
        message: str = "success",
    ) -> "ApiResponse":
        total_pages = max(1, math.ceil(total / per_page)) if per_page > 0 else 1
        return cls(
            code=200,
            message=message,
            data={
                "items": items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            },
        )

    # ── JSONResponse helper ────────────────────────────────────────────────

    def to_json_response(self, http_status: int = 200) -> JSONResponse:
        """
        Convert to a FastAPI JSONResponse with the given HTTP status code.
        The `code` field in the body is the application-level code;
        `http_status` is the transport-level HTTP status.
        """
        return JSONResponse(
            status_code=http_status,
            content=self.model_dump(),
        )


# ── Convenience type aliases ───────────────────────────────────────────────────


def success_response(data: Any = None, message: str = "success") -> JSONResponse:
    return ApiResponse.ok(data=data, message=message).to_json_response(200)


def error_response(code: int, message: str, http_status: int = None, data: Any = None) -> JSONResponse:
    """
    Return a standardized error JSONResponse.
    http_status defaults to code if not provided (capped at valid range).
    """
    if http_status is None:
        http_status = code if 100 <= code <= 599 else 500
    return ApiResponse.error(code=code, message=message, data=data).to_json_response(http_status)
