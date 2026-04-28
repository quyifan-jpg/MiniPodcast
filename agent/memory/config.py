"""
Configuration for the multi-layer memory system.

All settings are prefixed with MEMORY_ in environment variables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class MemorySettings(BaseSettings):
    """Memory system configuration — all fields have sensible defaults."""

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        case_sensitive=False,
    )

    # ── Layer 1: Sliding Window ──────────────────────────────────────
    window_size: int = 5
    """Number of recent conversation turns to keep in full (Agno num_history_runs)."""

    # ── Layer 2: Summary Memory ──────────────────────────────────────
    summary_enabled: bool = True
    """Enable LLM-based conversation summarization for older messages."""

    summary_max_words: int = 200
    """Target summary length in words."""

    summary_cache_ttl_s: int = 300
    """Redis cache TTL for summaries (5 minutes)."""

    summary_trigger_threshold: int = 8
    """Number of total turns before triggering summarization.
    If total turns <= this, no summary is generated (window covers all)."""

    # ── Layer 3: User Preferences ────────────────────────────────────
    preferences_enabled: bool = True
    """Enable cross-session user preference extraction and injection."""

    preferences_cache_ttl_s: int = 600
    """Redis cache TTL for user preferences (10 minutes)."""

    # ── Layer 4: Content History ─────────────────────────────────────
    history_enabled: bool = True
    """Enable injection of recent podcast history into context."""

    history_max_items: int = 5
    """Maximum number of recent podcasts to include in context."""

    history_days_back: int = 30
    """How many days back to look for podcast history."""


memory_settings = MemorySettings()
