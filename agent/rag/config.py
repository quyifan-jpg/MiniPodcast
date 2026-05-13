"""
RAG retrieval engine configuration.

Equivalent to CAgent's SearchChannelProperties (YAML-based config).
All values can be overridden via environment variables with the RAG_ prefix.

Usage:
    from rag.config import rag_settings
    rag_settings.chunk_vector_enabled
    rag_settings.rrf_k
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RAGSettings(BaseSettings):
    """RAG engine configuration with sensible defaults."""

    # ── Channel Toggles ───────────────────────────────────────────────
    chunk_vector_enabled: bool = True
    article_vector_enabled: bool = True
    keyword_enabled: bool = True
    social_media_enabled: bool = True
    external_search_enabled: bool = True

    # ── Channel Parameters ────────────────────────────────────────────
    top_k: int = Field(default=10, description="Final number of results to return")
    top_k_multiplier: int = Field(default=2, description="Multiplier for per-channel recall")
    channel_timeout_s: float = Field(default=30.0, description="Per-channel timeout in seconds")

    # ── Chunk Vector Channel ──────────────────────────────────────────
    chunk_min_similarity: float = Field(default=0.22, description="Min IP similarity for chunks (OpenAI embeddings)")
    chunk_max_per_article: int = Field(default=3, description="Max chunks kept per article")

    # ── Article Vector Channel ────────────────────────────────────────
    article_min_similarity: float = Field(default=0.35, description="Min IP similarity for articles (OpenAI embeddings)")

    # ── Social Media Channel ──────────────────────────────────────────
    social_media_days_back: int = Field(default=7, description="How many days back to search")

    # ── External Search Channel ───────────────────────────────────────
    google_news_max: int = Field(default=5, description="Max Google News results")
    duckduckgo_max: int = Field(default=8, description="Max DuckDuckGo results")

    # ── Post-Processor: Deduplication ─────────────────────────────────
    dedup_enabled: bool = True

    # ── Post-Processor: RRF ───────────────────────────────────────────
    rrf_enabled: bool = True
    rrf_k: int = Field(
        default=60,
        description="RRF smoothing parameter (Cormack et al. 2009). "
                    "Higher → more weight on multi-channel presence. "
                    "Lower → more weight on single-channel rank.",
    )

    # ── Post-Processor: Rerank ────────────────────────────────────────
    rerank_enabled: bool = False  # Disabled by default — requires API key
    rerank_provider: str = Field(default="cohere", description="cohere | jina | disabled")
    rerank_api_key: Optional[str] = Field(default=None, description="Rerank provider API key")
    rerank_model: str = Field(default="rerank-v3.5", description="Rerank model name")

    # ── Post-Processor: Quality Filter ────────────────────────────────
    quality_filter_enabled: bool = True
    quality_min_score: float = Field(default=0.3, description="Min score to keep")
    quality_min_length: int = Field(default=10, description="Min content length (chars)")
    quality_keep_best: bool = Field(default=True, description="Keep best chunk if all filtered")

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Module-level singleton
rag_settings = RAGSettings()
