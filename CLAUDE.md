# MiniBlog — Project Guide for Claude

## Project Overview

MiniBlog is an AI-powered podcast content aggregation system.
It collects articles and social media content, processes them via AI agents,
and generates personalized podcasts with TTS audio and cover images.

**Stack:** Python 3.11 · FastAPI · Celery · Redis · MySQL · React 19

---

## Quick Start

```bash
# Option A: Docker (recommended)
make start          # starts MySQL + Redis + API + Celery + Scheduler
make health         # verify all services are healthy

# Option B: Local development
cp agent/.env.example agent/.env   # fill in your API keys
make dev            # hot-reload API at http://localhost:8000
make celery         # in another terminal
make scheduler      # in another terminal
```

---

## Directory Structure

```
miniblog-go/
├── agent/                    # Python backend (FastAPI + Celery)
│   ├── core/                 # Infrastructure layer (NEW)
│   │   ├── config.py         # Pydantic Settings — all env vars live here
│   │   ├── logging.py        # loguru structured logging + ContextVar
│   │   ├── exceptions.py     # Exception hierarchy (Client/Service/Remote)
│   │   ├── response.py       # ApiResponse[T] standardized envelope
│   │   └── exception_handler.py  # FastAPI global exception handlers
│   ├── middleware/           # HTTP middleware layer (NEW)
│   │   ├── request_logging.py    # Request/response logging + X-Request-ID
│   │   ├── rate_limit.py         # Redis sliding-window rate limiting
│   │   └── auth.py               # JWT authentication
│   ├── decorators/           # Business decorators (NEW)
│   │   ├── circuit_breaker.py    # 3-state circuit breaker for LLM calls
│   │   └── trace.py              # @trace_root / @trace_node distributed tracing
│   ├── routers/              # FastAPI routers (HTTP layer)
│   ├── services/             # Business logic layer
│   ├── agents/               # LangGraph AI agents
│   ├── db/                   # SQLAlchemy models + database helpers
│   │   └── mixins.py         # TimestampMixin, SoftDeleteMixin (NEW)
│   ├── models/               # Pydantic request/response schemas
│   ├── tools/                # Scraping/search utilities
│   ├── processors/           # Content processing pipelines
│   ├── migrations/           # Alembic database migrations (NEW)
│   ├── tests/                # Test suite
│   ├── main.py               # FastAPI app entry point
│   ├── alembic.ini           # Alembic config (NEW)
│   ├── ruff.toml             # Linting config
│   └── requirements.txt      # Python dependencies
├── web/                      # React 19 frontend (JavaScript + Tailwind)
├── infra/                    # Terraform AWS infrastructure
├── Makefile                  # Development task runner (NEW)
├── CLAUDE.md                 # This file
└── .github/workflows/ci.yml  # GitHub Actions CI (NEW)
```

---

## Architecture

```
HTTP Request
    ↓
[RequestLoggingMiddleware]    — Injects X-Request-ID, logs timing
    ↓
[RateLimitMiddleware]         — Redis ZSET sliding window (100 req/60s)
    ↓
[AuthMiddleware]              — JWT verification (protected routes only)
    ↓
[FastAPI Router]              — Pydantic validation, route dispatch
    ↓
[Service Layer]               — Business logic (async methods, singletons)
    ↓
[DB / Redis / LLM / S3]       — External dependencies
    ↓
[GlobalExceptionHandler]      — Converts all exceptions → ApiResponse{code,message,data}
    ↓
HTTP Response
```

### Background Processing

```
API → celery_tasks.agent_chat → [agent queue] → Celery Worker
                                                       ↓
                                              SessionLockedTask
                                              (Redis distributed lock)
                                                       ↓
                                              LangGraph Agent Pipeline
                                              (with circuit breakers on LLM calls)
```

---

## Key Patterns

### 1. Configuration — `core/config.py`
All environment variables are defined as `Settings` fields with type validation.
**Never** use `os.environ.get()` directly; always import `settings`:
```python
from core.config import settings
settings.redis_host
settings.openai_api_key
```

### 2. Error Handling — `core/exceptions.py`
Raise domain exceptions, not FastAPI `HTTPException`:
```python
from core.exceptions import NotFoundException, ClientException, RemoteException

raise NotFoundException("Article")                     # 404
raise ClientException("Invalid page number", code=400) # 400
raise RemoteException(upstream="openai", message="...")  # 502
```
The global exception handler in `core/exception_handler.py` translates these to
standardized `ApiResponse` JSON responses automatically.

### 3. Logging — `core/logging.py`
Use loguru everywhere. **Never** use `print()` or `logging.*`:
```python
from loguru import logger

logger.info("Processing {count} articles", count=len(articles))
logger.error("Failed to generate podcast: {error}", error=str(e))
logger.warning("Cache miss for key: {key}", key=cache_key)
```
In production (APP_ENV=production), logs are emitted as JSON.
In development (APP_ENV=development), logs are colourised human-readable.

### 4. Circuit Breaker — `decorators/circuit_breaker.py`
Apply to all LLM API calls to prevent cascade failures:
```python
from decorators.circuit_breaker import openai_breaker

@openai_breaker
async def call_openai(prompt: str) -> str:
    return await client.chat.completions.create(...)
```

### 5. Distributed Tracing — `decorators/trace.py`
```python
from decorators.trace import trace_root, trace_node

@trace_root(name="podcast_generation")   # generates trace_id
async def generate_podcast(session_id: str):
    script = await write_script(session_id)
    audio  = await generate_audio(script)

@trace_node(name="write_script")         # records span within trace
async def write_script(session_id: str) -> str:
    ...
```

### 6. Soft Delete — `db/mixins.py`
```python
from db.mixins import TimestampMixin, SoftDeleteMixin

class Article(Base, TimestampMixin, SoftDeleteMixin):
    ...

# Query only active records
stmt = select(Article).where(Article.active())

# Soft delete (don't call session.delete!)
article.soft_delete()
session.commit()
```

### 7. Standardized API Response
Every endpoint should return `ApiResponse`:
```python
from core.response import ApiResponse

return ApiResponse.ok(data=article.dict())
return ApiResponse.paginated(items=articles, total=100, page=1, per_page=10)
```

---

## Environment Variables

All variables are defined in `core/config.py` → `Settings` class.
See that file for full list, types, and defaults.

Required for production:
```
DATABASE_URL=mysql+pymysql://user:pass@host:3306/miniblog
REDIS_PASSWORD=<password>
JWT_SECRET_KEY=<random-256-bit-key>
OPENAI_API_KEY=sk-...
ELEVENSLAB_API_KEY=sk_...
ALLOWED_ORIGINS=["https://yourdomain.com"]
```

---

## API Endpoints

| Version | Base URL | Notes |
|---------|----------|-------|
| v1 (current) | `/api/v1/` | Use this |
| v0 (deprecated) | `/api/` | Backward compat alias, remove 2026-07-01 |

Key endpoints:
- `GET /health` — Docker liveness probe
- `GET /health/detail` — Readiness probe with component status
- `GET /metrics` — Prometheus metrics
- `GET /docs` — Swagger UI (development only)
- `GET /api/v1/articles` — Paginated article list
- `GET /api/v1/podcasts` — Podcast list
- `POST /api/v1/podcast-agent/chat` — AI conversation (requires JWT)

---

## Database Migrations

```bash
# Apply all pending migrations
make migrate

# Check current state
make migrate-status

# Create a new migration (after changing models)
cd agent && alembic revision --autogenerate -m "add_field_xyz"

# Roll back one step
make migrate-rollback
```

---

## Running Tests

```bash
make test           # run all tests
make test-cov       # run with coverage report
```

Tests live in `agent/tests/`. There is no `conftest.py` yet — add fixtures there as needed.

---

## Code Quality

```bash
make lint           # check (no changes)
make format         # auto-fix
```

Pre-commit hooks are configured in `agent/.pre-commit-config.yaml`.
Install once: `make setup`.

---

## Celery Task Queues

| Queue | Purpose | Priority |
|-------|---------|----------|
| `agent` | AI conversation tasks | High |
| `crawl` | Web scraping and ingestion | Medium |
| `media` | Audio/image generation | Low (resource-intensive) |
| `default` | Catch-all | Normal |

Start workers: `make celery` (runs all queues).

---

## Coding Conventions

1. **Async-first**: All service methods are `async def`
2. **Service singletons**: `article_service = ArticleService()` at module level
3. **Pydantic v2**: Use `model_config = ConfigDict(...)` not `class Config:`
4. **No `print()`**: Use `from loguru import logger` everywhere
5. **No `os.environ.get()`**: Use `from core.config import settings`
6. **Domain exceptions**: Raise `ClientException`/`ServiceException`/`RemoteException`,
   not `HTTPException` (except in router layer where necessary)
7. **Type hints**: Add type hints to all new function signatures

---

## Architecture Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Python vs Go | Python | AI/ML ecosystem (LangChain, transformers, etc.) |
| FastAPI vs Flask | FastAPI | Async-native, Pydantic v2, auto-docs |
| Celery vs asyncio tasks | Celery | Distributed, retries, queue isolation |
| PyMySQL vs SQLAlchemy ORM | Hybrid | Performance + legacy schema compatibility |
| loguru vs stdlib logging | loguru | Structured JSON, ContextVar support, cleaner API |
| pydantic-settings vs dotenv | pydantic-settings | Type validation, computed properties |
