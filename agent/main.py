"""
MiniBlog API — Application entry point.

Production-grade setup following CAgent patterns:
- Centralized config via Pydantic Settings (core.config)
- Structured logging via loguru (core.logging)
- Standardized error handling (core.exception_handler)
- Health check endpoints (/health, /health/detail)
- Prometheus metrics (/metrics)
- Secure CORS configuration
- API versioning (/api/v1/)
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

import aiofiles
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# ── Core infrastructure (must be imported before other app modules) ────────────
from core.config import settings
from core.exception_handler import register_exception_handlers
from core.logging import setup_logging

# ── Routers ───────────────────────────────────────────────────────────────────
from routers import (
    article_router,
    async_podcast_agent_router,
    podcast_config_router,
    podcast_router,
    social_media_router,
    source_router,
    task_router,
)
from services.db_init import init_databases


# ── Prometheus metrics (optional — degrades gracefully if not installed) ───────
# uvicorn --reload / debugger can import this module twice in one process; registering
# the same metric names twice raises ValueError from prometheus_client.
def _prometheus_get_or_create(factory, name: str, documentation: str, labelnames: list[str]):
    from prometheus_client import REGISTRY

    try:
        return factory(name, documentation, labelnames)
    except ValueError:
        for key, collector in REGISTRY._names_to_collectors.items():
            if key == name or key.startswith(f"{name}_"):
                return collector
        raise


try:
    from prometheus_fastapi_instrumentator import Instrumentator
    from prometheus_client import Counter, Gauge, Histogram

    _prometheus_available = True

    celery_tasks_total = _prometheus_get_or_create(
        Counter,
        "miniblog_celery_tasks_total",
        "Total Celery tasks processed",
        ["task_name", "status"],
    )
    llm_request_duration = _prometheus_get_or_create(
        Histogram,
        "miniblog_llm_request_seconds",
        "LLM API call duration in seconds",
        ["provider"],
    )
    active_sessions = _prometheus_get_or_create(
        Gauge,
        "miniblog_active_sessions",
        "Number of active podcast sessions",
        [],
    )
except ImportError:
    _prometheus_available = False
    logger.warning(
        "prometheus-fastapi-instrumentator not installed. "
        "Run: pip install prometheus-fastapi-instrumentator"
    )


def _register_plain_prometheus_metrics_route() -> None:
    """Expose /metrics without Instrumentator (faster under debuggers)."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from fastapi import Response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup structured logging first
    setup_logging(app_env=settings.app_env)

    logger.info(
        "Starting MiniBlog API | env={env} | debug={debug}",
        env=settings.app_env,
        debug=settings.debug,
    )

    # Ensure required local directories exist
    for directory in [
        "databases",
        "browsers",
        "podcasts/audio",
        "podcasts/images",
        "podcasts/recordings",
    ]:
        os.makedirs(directory, exist_ok=True)

    # Initialize database schema
    await init_databases()

    if not os.path.exists(settings.client_build_path):
        logger.warning(
            "React build path not found: {path}",
            path=settings.client_build_path,
        )

    logger.info("Application startup complete")
    yield

    logger.info("Shutting down application...")
    logger.info("Shutdown complete")


# ── App factory ────────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    description="MiniBlog API — AI-powered podcast content aggregation",
    version="1.0.0",
    lifespan=lifespan,
    # Hide /docs and /redoc in production
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ── Exception handlers ─────────────────────────────────────────────────────────
register_exception_handlers(app)

# ── Middleware ─────────────────────────────────────────────────────────────────
# Order matters: middleware is applied in reverse registration order (last-in, first-out).

# 1. Request logging middleware (import after app creation to avoid circular imports)
from middleware.request_logging import RequestLoggingMiddleware  # noqa: E402
app.add_middleware(RequestLoggingMiddleware)

# 2. CORS — Fixed: allow_origins=["*"] + allow_credentials=True is rejected by browsers.
#    Use explicit origin list from settings.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Session-ID"],
    expose_headers=["X-Request-ID"],
)

# ── Prometheus instrumentation ─────────────────────────────────────────────────
# Instrumentator.instrument(app) introspects all routes at import time; under
# Cursor/VSCode debugpy this often appears "stuck" here. Use:
#   PROMETHEUS_INSTRUMENT_HTTP=false
# to keep /metrics (custom counters) without HTTP middleware wiring.
if _prometheus_available and settings.prometheus_instrument_http:
    try:
        Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated_routes=True,
            excluded_handlers=["/health", "/health/detail", "/metrics", "/favicon.ico"],
        ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    except Exception as e:
        logger.warning(
            "Prometheus Instrumentator failed ({e}); falling back to plain /metrics",
            e=e,
        )
        _register_plain_prometheus_metrics_route()
elif _prometheus_available:
    _register_plain_prometheus_metrics_route()
else:
    # 即便没装 prometheus_fastapi_instrumentator，也用 prometheus_client 暴露
    # 业务模块自己注册的指标（比如 article_service 里的那些）
    try:
        _register_plain_prometheus_metrics_route()
    except ImportError:
        pass


# ── Health check endpoints ─────────────────────────────────────────────────────
# Fixes the broken docker-compose healthcheck that references /health but
# the endpoint was never implemented.

@app.get("/health", tags=["ops"], include_in_schema=False)
async def health_check() -> Dict[str, Any]:
    """
    Fast liveness probe for docker-compose / load-balancer health checks.
    Returns immediately without checking dependencies.
    """
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "env": settings.app_env,
    }


@app.get("/health/detail", tags=["ops"], include_in_schema=False)
async def health_detail() -> Dict[str, Any]:
    """
    Detailed readiness probe — checks each downstream dependency.
    Equivalent to CAgent's /actuator/health with component breakdown.
    """
    components: Dict[str, Any] = {}
    overall_healthy = True

    # Check MySQL
    try:
        import asyncio
        from services.db_service import tracking_db
        await asyncio.wait_for(
            tracking_db.execute_query("SELECT 1", fetch_one=True),
            timeout=3.0,
        )
        components["mysql"] = {"status": "up"}
    except Exception as e:
        components["mysql"] = {"status": "down", "error": str(e)[:100]}
        overall_healthy = False

    # Check Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(**{k: v for k, v in settings.redis_kwargs.items()})
        await r.ping()
        await r.aclose()
        components["redis"] = {"status": "up"}
    except Exception as e:
        components["redis"] = {"status": "down", "error": str(e)[:100]}
        overall_healthy = False

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "env": settings.app_env,
        "components": components,
    }


# ── Routers — versioned (/api/v1/) + backward-compat aliases ──────────────────

# v1 versioned routes (canonical)
app.include_router(article_router.router, prefix="/api/v1/articles", tags=["articles"])
app.include_router(source_router.router, prefix="/api/v1/sources", tags=["sources"])
app.include_router(podcast_router.router, prefix="/api/v1/podcasts", tags=["podcasts"])
app.include_router(task_router.router, prefix="/api/v1/tasks", tags=["tasks"])
app.include_router(podcast_config_router.router, prefix="/api/v1/podcast-configs", tags=["podcast-configs"])
app.include_router(async_podcast_agent_router.router, prefix="/api/v1/podcast-agent", tags=["podcast-agent"])
app.include_router(social_media_router.router, prefix="/api/v1/social-media", tags=["social-media"])

# Backward-compat aliases (deprecated — remove after 2026-07-01)
app.include_router(article_router.router, prefix="/api/articles", tags=["articles-v0"], include_in_schema=False)
app.include_router(source_router.router, prefix="/api/sources", tags=["sources-v0"], include_in_schema=False)
app.include_router(podcast_router.router, prefix="/api/podcasts", tags=["podcasts-v0"], include_in_schema=False)
app.include_router(task_router.router, prefix="/api/tasks", tags=["tasks-v0"], include_in_schema=False)
app.include_router(podcast_config_router.router, prefix="/api/podcast-configs", tags=["podcast-configs-v0"], include_in_schema=False)
app.include_router(async_podcast_agent_router.router, prefix="/api/podcast-agent", tags=["podcast-agent-v0"], include_in_schema=False)
app.include_router(social_media_router.router, prefix="/api/social-media", tags=["social-media-v0"], include_in_schema=False)


# ── Streaming endpoints (audio / video with HTTP Range support) ────────────────

@app.get("/stream-audio/{filename}")
async def stream_audio(filename: str, request: Request):
    audio_path = os.path.join("podcasts/audio", filename)
    if not os.path.exists(audio_path):
        return Response(status_code=404, content="Audio file not found")
    file_size = os.path.getsize(audio_path)
    range_header = request.headers.get("Range", "").strip()
    start = 0
    end = file_size - 1
    if range_header:
        try:
            range_data = range_header.replace("bytes=", "").split("-")
            start = int(range_data[0]) if range_data[0] else 0
            end = int(range_data[1]) if len(range_data) > 1 and range_data[1] else file_size - 1
        except ValueError:
            return Response(status_code=400, content="Invalid range header")
    end = min(end, file_size - 1)
    content_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
        "Content-Disposition": f"inline; filename={filename}",
        "Content-Type": "audio/wav",
    }

    async def file_streamer():
        async with aiofiles.open(audio_path, "rb") as f:
            await f.seek(start)
            remaining = content_length
            chunk_size = 64 * 1024
            while remaining > 0:
                chunk = await f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    status_code = 206 if range_header else 200
    return StreamingResponse(file_streamer(), status_code=status_code, headers=headers)


@app.get("/stream-recording/{session_id}/{filename}")
async def stream_recording(session_id: str, filename: str, request: Request):
    recording_path = os.path.join("podcasts/recordings", session_id, filename)
    if not os.path.exists(recording_path):
        return Response(status_code=404, content="Recording video not found")
    file_size = os.path.getsize(recording_path)
    range_header = request.headers.get("Range", "").strip()
    start = 0
    end = file_size - 1
    if range_header:
        try:
            range_data = range_header.replace("bytes=", "").split("-")
            start = int(range_data[0]) if range_data[0] else 0
            end = int(range_data[1]) if len(range_data) > 1 and range_data[1] else file_size - 1
        except ValueError:
            return Response(status_code=400, content="Invalid range header")
    end = min(end, file_size - 1)
    content_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
        "Content-Disposition": f"inline; filename={filename}",
        "Content-Type": "video/webm",
    }

    async def file_streamer():
        async with aiofiles.open(recording_path, "rb") as f:
            await f.seek(start)
            remaining = content_length
            chunk_size = 64 * 1024
            while remaining > 0:
                chunk = await f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    status_code = 206 if range_header else 200
    return StreamingResponse(file_streamer(), status_code=status_code, headers=headers)


# ── Static files ───────────────────────────────────────────────────────────────

app.mount("/audio", StaticFiles(directory="podcasts/audio"), name="audio")
app.mount("/server_static", StaticFiles(directory="static"), name="server_static")
app.mount("/podcast_img", StaticFiles(directory="podcasts/images"), name="podcast_img")

_client_static = os.path.join(settings.client_build_path, "static")
if os.path.exists(_client_static):
    app.mount("/static", StaticFiles(directory=_client_static), name="react_static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = os.path.join(settings.client_build_path, "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    return Response(status_code=404)


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    manifest_path = os.path.join(settings.client_build_path, "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path)
    return Response(status_code=404)


@app.get("/logo{rest_of_path:path}", include_in_schema=False)
async def logo(rest_of_path: str):
    logo_path = os.path.join(settings.client_build_path, f"logo{rest_of_path}")
    if os.path.exists(logo_path):
        return FileResponse(logo_path)
    return Response(status_code=404)


# ── React SPA fallback ─────────────────────────────────────────────────────────

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_react(full_path: str, request: Request):
    if full_path.startswith("api/") or request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"code": 404, "message": "Not found"})
    index_path = os.path.join(settings.client_build_path, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(
        status_code=503,
        content={
            "code": 503,
            "message": "React client not found. Build the client or set CLIENT_BUILD_PATH.",
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.is_development,
        timeout_keep_alive=120,
        timeout_graceful_shutdown=120,
    )
