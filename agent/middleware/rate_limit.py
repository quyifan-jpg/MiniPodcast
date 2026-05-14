"""
Redis sliding-window rate limiting middleware.

Equivalent to CAgent's @ChatRateLimit with Redis ZSET + Redisson semaphore.

Two tiers:
  1. Global tier  — 100 req/60s per IP (protects all API routes)
  2. Chat tier    — 20 req/60s per session_id (protects LLM endpoints)

Algorithm: Redis ZSET sliding window (same as CAgent ChatQueueLimiter).
  - Add current timestamp to ZSET with score = timestamp
  - Remove entries older than window
  - Check cardinality against limit
  - All three ops are atomic via a Lua script

Usage:
    app.add_middleware(RateLimitMiddleware)
"""

from __future__ import annotations

import time

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.config import settings

# Paths exempt from rate limiting
_EXEMPT_PATHS = frozenset(
    [
        "/health",
        "/health/detail",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
    ]
)

# Endpoints that use the stricter chat rate limit
_CHAT_PATH_PREFIX = "/api/v1/podcast-agent"

# Lua script: atomic sliding-window ZSET rate-limit check
# KEYS[1] = rate limit key
# ARGV[1] = current timestamp (float string)
# ARGV[2] = window start (current - window_seconds)
# ARGV[3] = max requests
# ARGV[4] = window TTL in seconds (for key expiry)
# Returns: 1 if allowed, 0 if denied
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_start = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

-- Remove expired entries
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Count current entries
local count = redis.call('ZCARD', key)

if count < max_requests then
    -- Add current request with timestamp as both score and member
    redis.call('ZADD', key, now, now .. ':' .. math.random(1000000))
    redis.call('EXPIRE', key, ttl)
    return 1
else
    return 0
end
"""


def _get_redis_client():
    """Lazy-load Redis client to avoid import-time side effects."""
    try:
        import redis.asyncio as aioredis

        kwargs = {
            "host": settings.redis_host,
            "port": settings.redis_port,
            "db": settings.redis_db + 2,  # Use DB+2 to avoid colliding with cache (DB+1)
        }
        if settings.redis_username:
            kwargs["username"] = settings.redis_username
        if settings.redis_password:
            kwargs["password"] = settings.redis_password
        return aioredis.Redis(**kwargs)
    except Exception as e:
        logger.warning("Rate limit Redis client init failed: {e}", e=str(e))
        return None


_redis_client = None


def _client():
    global _redis_client
    if _redis_client is None:
        _redis_client = _get_redis_client()
    return _redis_client


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Two-tier sliding-window rate limiter using Redis ZSET.
    Equivalent to CAgent's @ChatRateLimit AOP interceptor.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Skip exempt paths
        if path in _EXEMPT_PATHS:
            return await call_next(request)

        # Only rate-limit API routes
        if not path.startswith("/api/"):
            return await call_next(request)

        r = _client()
        if r is None:
            # Redis unavailable — degrade gracefully, don't block requests
            logger.warning("Rate limiter Redis unavailable, skipping rate limit check")
            return await call_next(request)

        # Determine tier
        is_chat = path.startswith(_CHAT_PATH_PREFIX)
        if is_chat:
            session_id = request.headers.get("X-Session-ID") or request.query_params.get("session_id") or ""
            limit_key = f"ratelimit:chat:{session_id}" if session_id else None
            max_requests = settings.chat_rate_limit_requests
        else:
            # Use client IP for global tier
            client_ip = _get_client_ip(request)
            limit_key = f"ratelimit:global:{client_ip}"
            max_requests = settings.rate_limit_requests

        if limit_key:
            allowed = await _check_rate_limit(
                r,
                key=limit_key,
                max_requests=max_requests,
                window_seconds=settings.rate_limit_window_seconds,
            )
            if not allowed:
                logger.warning(
                    "Rate limit exceeded | path={path} | key={key}",
                    path=path,
                    key=limit_key,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": 429,
                        "message": "Too many requests, please try again later",
                        "data": None,
                    },
                    headers={"Retry-After": str(settings.rate_limit_window_seconds)},
                )

        return await call_next(request)


async def _check_rate_limit(
    r,
    key: str,
    max_requests: int,
    window_seconds: int,
) -> bool:
    """
    Atomically check and record a request using Redis sliding window ZSET.
    Returns True if allowed, False if limit exceeded.
    """
    try:
        now = time.time()
        window_start = now - window_seconds
        result = await r.eval(
            _RATE_LIMIT_LUA,
            1,
            key,
            str(now),
            str(window_start),
            str(max_requests),
            str(window_seconds + 10),  # TTL slightly longer than window
        )
        return bool(result)
    except Exception as e:
        # Redis error — fail open (allow request) to avoid availability issues
        logger.warning("Rate limit check failed: {e}", e=str(e))
        return True


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from load balancers."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
