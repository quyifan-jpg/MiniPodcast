# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**MiniBlog** is an AI-powered podcast generation platform. It ingests content from RSS feeds, social media (X.com, Facebook), and web URLs, then runs a multi-stage pipeline — search → scrape/verify → script → image → audio — to produce complete podcast episodes. The backend is Python/FastAPI; the frontend is React.

## Common Commands

All commands run from the repo root unless otherwise noted.

### Docker (recommended)
```bash
make start          # Start all services (MySQL, Redis, API, Celery, Scheduler)
make stop           # Stop all services
make restart        # Restart API + workers only (keep infra)
make status         # Show container health + curl /health
make logs           # Tail API logs
make logs-celery    # Tail Celery logs
```

### Local development (no Docker)
```bash
cd agent
source ../venv/bin/activate

uvicorn main:app --reload --port 8000   # or: make dev
python celery_worker.py                 # or: make celery
python scheduler.py                     # or: make scheduler
```

### Frontend
```bash
cd web && npm install && npm start      # Dev server on :3000
```

### Testing & linting
```bash
make test           # pytest agent/tests/ -v --tb=short
make test-cov       # + coverage report
make lint           # ruff check + ruff format --check
make format         # ruff format + isort (auto-fix)
```

### Database migrations (Alembic)
```bash
make migrate            # alembic upgrade head
make migrate-status     # alembic current
make migrate-rollback   # alembic downgrade -1
```

### Bootstrap demo data
```bash
cd agent && python bootstrap_demo.py
```

## Architecture

### Services (Docker Compose — `agent/docker-compose.yml`)
| Service | Description |
|---|---|
| `api` | FastAPI server on :8000; serves REST API + React build |
| `celery` | Celery workers (2 replicas, thread pool) for async podcast tasks |
| `scheduler` | APScheduler process — triggers recurring processor tasks |
| `mysql` | Primary datastore (MySQL 8) |
| `redis` | Celery broker/backend + caching |

### Backend layout (`agent/`)
```
main.py              — FastAPI app, lifespan startup, static file mounts, audio streaming
core/config.py       — Pydantic Settings singleton; all env vars imported from here
routers/             — FastAPI route handlers (articles, sources, podcasts, tasks, social-media, podcast-agent)
services/
  celery_app.py      — Celery app + SessionLockedTask base (prevents concurrent runs per session)
  celery_tasks.py    — Main Agno agent task (agent_chat); podcast pipeline entry point
  model_router.py    — Strategy + circuit-breaker provider selection (OpenAI → Anthropic failover)
  db_init.py         — DB initialisation on startup
agents/              — Agno-framework agent wrappers (search, scrape, script, image, audio)
graph/
  search_scrape_graph.py — LangGraph pipelines: ReAct search loop + parallel URL verification
  state.py           — TypedDict state schemas for LangGraph
  tools_registry.py  — Wraps project tools as LangChain tools for the ReAct agent
processors/          — Background batch processors (RSS, URL, AI analysis, embeddings, FAISS, podcast, social scrapers)
tools/               — Individual agent tools (web search, embedding search, chunk search, browser crawler, etc.)
db/
  agent_config_v2.py — Agent instructions, description, model name, initial session state
  *.py               — SQLite/MySQL query helpers per domain
models/              — Pydantic request/response schemas
memory/              — Agent memory: config, manager, store, summariser
rag/bridge.py        — RAG retrieval bridge
decorators/
  circuit_breaker.py — CircuitBreaker with CLOSED/HALF_OPEN/OPEN states; shared openai_breaker/anthropic_breaker singletons
  trace.py           — Distributed tracing decorator
middleware/          — Auth, rate limiting, request logging
```

### Podcast pipeline (high-level)
1. **User chat** → Celery task `agent_chat` → **Agno orchestrator** (in `celery_tasks.py`)
2. Orchestrator calls tools in sequence:
   - `search_agent_run` → `graph/search_scrape_graph.py:run_search_pipeline()` (LangGraph ReAct)
   - `scrape_agent_run` → `run_parallel_verify()` (LangGraph fan-out via `Send()`)
   - `user_source_selection_run` → user picks sources
   - `podcast_script_agent_run` → generates script
   - `image_generation_agent_run` → banner image
   - `audio_generate_agent_run` → TTS (OpenAI / ElevenLabs / Kokoro)
3. Session state persists to MySQL via `agno.storage.singlestore.SingleStoreStorage`

### Model routing
`services/model_router.py` exposes a module-level `router` singleton. Call `router.get_chat_model()` for LangChain models or `router.get_agno_model()` for Agno models. Priority order: `gpt-4o-mini` → `gpt-4o` → `claude-sonnet-4-20250514`. Circuit breakers (`decorators/circuit_breaker.py`) automatically skip degraded providers.

### Adding a new content processor
1. Create `processors/my_processor.py` with a `process_*(...)` function returning `{"processed": N, "success": N, "errors": N}`.
2. Register the `TaskType` enum value and metadata in `models/tasks_schemas.py`.

### Adding a new agent tool
1. Create `tools/my_tool.py` — function signature `(agent: Agent, ...) -> str`.
2. Import and add to the `tools=[...]` list in `services/celery_tasks.py`.

## Environment Variables

Create `agent/.env` (loaded by `python-dotenv`; also read by Pydantic Settings):

```
OPENAI_API_KEY=...
ELEVENSLAB_API_KEY=...          # optional, for ElevenLabs TTS
ANTHROPIC_API_KEY=...           # optional, enables Anthropic failover
DATABASE_URL=mysql+pymysql://root:root@localhost:3306/miniblog
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=root
```

All settings are documented in `agent/core/config.py`.

## Key Constraints

- **Social media processors share one persistent browser session** (`browsers/playwright_persistent_profile_web`). Do not run X.com and Facebook scrapers concurrently — stagger schedules by ≥30 min.
- **No authentication layer yet** — the API is open; do not expose port 8000 publicly without a proxy.
- Generated media (audio, images, recordings) lives in `agent/podcasts/`; databases in `agent/databases/`. Both are volume-mounted in Docker.
- The React build is served directly by FastAPI from `../web/build` (env `CLIENT_BUILD_PATH`). Run `npm run build` in `web/` before starting the backend without the dev server.
