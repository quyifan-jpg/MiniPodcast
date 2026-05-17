"""
Centralized configuration management via Pydantic Settings.

Equivalent to CAgent's application.yaml with profile support.
All os.environ.get() calls across the codebase should import from here.

Usage:
    from core.config import settings
    settings.redis_host
    settings.database_url
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── App ────────────────────────────────────────────────────────────────
    app_name: str = "MiniBlog API"
    app_env: str = "production"  # development | staging | production
    debug: bool = False
    port: int = 8000

    # ── Database ───────────────────────────────────────────────────────────
    database_url: str = Field(
        default="mysql+pymysql://root:root@localhost:3306/miniblog",
        description="MySQL connection string",
    )

    # ── Redis ──────────────────────────────────────────────────────────────
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str | None = None
    redis_username: str | None = None
    redis_db: int = 0

    # ── Auth ───────────────────────────────────────────────────────────────
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30  # short-lived access token (30 min)
    jwt_refresh_expire_minutes: int = 60 * 24 * 14  # long-lived refresh token (14 days)

    # ── CORS ───────────────────────────────────────────────────────────────
    # Override via env: ALLOWED_ORIGINS='["http://localhost:3000","https://myapp.com"]'
    allowed_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:18000",
        "http://127.0.0.1:3000",
    ]

    # ── Frontend ───────────────────────────────────────────────────────────
    client_build_path: str = "../web/build"

    # ── AWS ────────────────────────────────────────────────────────────────
    aws_s3_bucket: str = ""
    aws_region: str = "us-east-1"

    # ── LLM / External APIs ────────────────────────────────────────────────
    openai_api_key: str = ""
    elevenslab_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    minimax_api_key: str = ""

    # ── Rate limiting ──────────────────────────────────────────────────────
    rate_limit_requests: int = 100  # default requests per window
    rate_limit_window_seconds: int = 60
    chat_rate_limit_requests: int = 20  # stricter limit for LLM endpoints

    # ── Celery ─────────────────────────────────────────────────────────────
    celery_worker_concurrency: int = 4
    celery_task_time_limit: int = 600
    celery_task_soft_time_limit: int = 540

    # ── Circuit Breaker ────────────────────────────────────────────────────
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: int = 60

    # ── Observability ───────────────────────────────────────────────────────
    # prometheus_fastapi_instrumentator walks every route at import time; under
    # debugpy this can look like a hang. Set PROMETHEUS_INSTRUMENT_HTTP=false to skip.
    prometheus_instrument_http: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown env vars
    )

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"app_env must be one of {allowed}, got '{v}'")
        return v

    # ── Computed helpers ───────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def redis_url(self) -> str:
        """Full Redis URL for Celery broker/backend."""
        if self.redis_username and self.redis_password:
            return f"redis://{self.redis_username}:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def redis_kwargs(self) -> dict:
        """Keyword arguments for redis.Redis() / aioredis connections."""
        kwargs: dict = {
            "host": self.redis_host,
            "port": self.redis_port,
            "db": self.redis_db,
        }
        if self.redis_username:
            kwargs["username"] = self.redis_username
        if self.redis_password:
            kwargs["password"] = self.redis_password
        return kwargs


# Module-level singleton — import this everywhere
settings = Settings()
