##
## MiniBlog — Development Task Runner
## Equivalent to CAgent's Makefile: make start/stop/status/dev/test/lint
##
## Usage:
##   make start     — Start all services via Docker Compose
##   make stop      — Stop all services
##   make dev       — Run API in local hot-reload mode
##   make test      — Run test suite
##   make lint      — Check code style
##   make format    — Auto-format code
##   make migrate   — Run database migrations
##   make health    — Check service health
##   make logs      — Tail API logs

.DEFAULT_GOAL := help
.PHONY: help start stop restart status dev celery scheduler test lint format migrate health logs setup clean

COMPOSE  := docker compose -f agent/docker-compose.yml
AGENT    := cd agent &&

# ── Docker Compose ─────────────────────────────────────────────────────────────

start: ## Start all services (Docker Compose)
	$(COMPOSE) up -d
	@echo ""
	@echo "✓ Services started. Run 'make status' to check health."
	@echo "  API:      http://localhost:8000"
	@echo "  API Docs: http://localhost:8000/docs"
	@echo "  Metrics:  http://localhost:8000/metrics"

stop: ## Stop all services
	$(COMPOSE) down

restart: ## Restart API and workers (keep infra running)
	$(COMPOSE) restart api celery scheduler

status: ## Show service status and health
	$(COMPOSE) ps
	@echo ""
	@echo "Health check:"
	@curl -sf http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "  API not reachable"

# ── Local Development ──────────────────────────────────────────────────────────

dev: ## Run API locally with hot reload (no Docker)
	$(AGENT) uvicorn main:app --reload --port 8000 --host 0.0.0.0

celery: ## Start Celery worker locally
	$(AGENT) python celery_worker.py

scheduler: ## Start APScheduler locally
	$(AGENT) python scheduler.py

# ── Testing ────────────────────────────────────────────────────────────────────

test: ## Run test suite
	$(AGENT) python -m pytest tests/ -v --tb=short

test-cov: ## Run tests with coverage report
	$(AGENT) python -m pytest tests/ -v --tb=short --cov=. --cov-report=term-missing --cov-report=html

# ── Code Quality ───────────────────────────────────────────────────────────────

lint: ## Check code style (ruff + isort --check)
	$(AGENT) ruff check .
	$(AGENT) ruff format --check .

format: ## Auto-format code (ruff + isort)
	$(AGENT) ruff format .
	$(AGENT) isort .

# ── Database ───────────────────────────────────────────────────────────────────

migrate: ## Run pending Alembic migrations
	$(AGENT) alembic upgrade head

migrate-status: ## Show current migration state
	$(AGENT) alembic current

migrate-history: ## Show migration history
	$(AGENT) alembic history --verbose

migrate-rollback: ## Rollback one migration step
	$(AGENT) alembic downgrade -1

# ── Operations ─────────────────────────────────────────────────────────────────

health: ## Check all service health endpoints
	@echo "=== Liveness ==="
	@curl -sf http://localhost:8000/health | python3 -m json.tool
	@echo ""
	@echo "=== Readiness ==="
	@curl -sf http://localhost:8000/health/detail | python3 -m json.tool

logs: ## Tail API container logs
	$(COMPOSE) logs -f api

logs-celery: ## Tail Celery worker logs
	$(COMPOSE) logs -f celery

metrics: ## Show Prometheus metrics (raw)
	@curl -sf http://localhost:8000/metrics | head -50

# ── Setup ──────────────────────────────────────────────────────────────────────

setup: ## Install dependencies and configure pre-commit hooks
	$(AGENT) pip install -r requirements.txt
	@if command -v pre-commit &> /dev/null; then \
		cd agent && pre-commit install; \
		echo "✓ pre-commit hooks installed"; \
	else \
		echo "⚠  pre-commit not found. Run: pip install pre-commit"; \
	fi

# ── Cleanup ────────────────────────────────────────────────────────────────────

clean: ## Stop all services, remove volumes, clean logs
	$(COMPOSE) down -v
	@find agent/logs -name "*.log" -delete 2>/dev/null || true
	@echo "✓ Clean complete"

# ── AWS Deployment ─────────────────────────────────────────────────────────────

deploy-backend: ## Build and push backend image to ECR
	@bash deploy-to-aws.sh

deploy-frontend: ## Build and deploy frontend to S3
	@bash deploy-frontend-to-aws.sh

# ── Help ───────────────────────────────────────────────────────────────────────

help: ## Show this help message
	@echo ""
	@echo "MiniBlog Development Commands"
	@echo "─────────────────────────────"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
