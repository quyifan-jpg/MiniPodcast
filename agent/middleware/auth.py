"""
JWT authentication middleware with progressive rollout.

Equivalent to CAgent's SaToken interceptor + UserContextInterceptor.

Progressive approach (avoids breaking existing open API):
  - Public paths: always allowed (health, metrics, docs, static assets)
  - Protected prefixes: require JWT (currently only /api/v1/podcast-agent)
  - Everything else: allowed (read-only article/podcast APIs remain open)

This allows gradual migration — tighten PROTECTED_PREFIXES over time.

FastAPI Dependency for route-level auth:
    from middleware.auth import get_current_user, require_auth

    @router.get("/sensitive")
    async def sensitive(user_id: str = Depends(require_auth)):
        ...
"""

from __future__ import annotations

from typing import Optional

import jwt
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.config import settings
from core.logging import set_user_id

# Paths that never require authentication
_PUBLIC_PATHS = frozenset(
    [
        "/health",
        "/health/detail",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
        "/manifest.json",
        "/api/auth/register",
        "/api/auth/login",
    ]
)

# Path prefixes that REQUIRE authentication
# Tighten this list gradually as the frontend adopts token auth.
_PROTECTED_PREFIXES = [
    "/api/auth/me",  # user profile — always needs token
    "/api/podcast-agent",  # chat sessions — always user-scoped
    # "/api/podcasts",       # uncomment to protect podcast management
    # "/api/tasks",          # uncomment to protect task management
]


class AuthMiddleware(BaseHTTPMiddleware):
    """
    JWT authentication middleware.
    Validates Bearer token from Authorization header.
    Injects user_id into request.state and logging context.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always pass through public paths and OPTIONS preflight
        if path in _PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        # Only enforce auth on protected prefixes
        if not any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
            return await call_next(request)

        # Extract token
        token = _extract_token(request)
        if not token:
            return _auth_error("Authentication required. Provide a Bearer token.")

        # Verify token
        payload = _verify_jwt(token)
        if payload is None:
            return _auth_error("Invalid or expired token.")

        # Inject user context
        user_id = str(payload.get("sub", ""))
        request.state.user_id = user_id
        set_user_id(user_id)

        return await call_next(request)


# ── FastAPI dependencies ───────────────────────────────────────────────────────


async def get_current_user(request: Request) -> Optional[str]:
    """
    FastAPI dependency that returns the authenticated user_id, or None
    if the request is unauthenticated (for optional-auth routes).
    """
    return getattr(request.state, "user_id", None)


async def require_auth(request: Request) -> str:
    """
    FastAPI dependency that REQUIRES authentication.
    Raises 401 if user is not authenticated.

    Usage:
        @router.get("/protected")
        async def handler(user_id: str = Depends(require_auth)):
            ...
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id


# ── Token helpers ──────────────────────────────────────────────────────────────


def create_access_token(user_id: str, extra_claims: dict = None) -> str:
    """
    Create a signed JWT access token.

    Usage:
        token = create_access_token(user_id="user_123")
        # Return to client, client sends as: Authorization: Bearer <token>
    """
    import time

    payload = {
        "sub": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + settings.jwt_expire_minutes * 60,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _extract_token(request: Request) -> Optional[str]:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def _verify_jwt(token: str) -> Optional[dict]:
    """Verify JWT signature and expiry. Returns payload dict or None."""
    if not settings.jwt_secret_key:
        # If no secret key configured, skip verification (dev mode)
        logger.warning("JWT_SECRET_KEY not set — skipping token verification")
        try:
            return jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return None
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        logger.warning("JWT token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid JWT token: {e}", e=str(e))
        return None


def _auth_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"code": 401, "message": message, "data": None},
        headers={"WWW-Authenticate": "Bearer"},
    )
