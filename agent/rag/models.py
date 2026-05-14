"""
Data models for the multi-channel retrieval engine.

Equivalent to CAgent's SearchChannelResult / SearchContext / RetrievedChunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MetadataKey:
    """
    Standard keys for RetrievedChunk.metadata.

    All channels MUST use these constants instead of raw strings so that
    post-processors (FreshnessBoost, QualityFilter) can reliably read them.
    """

    # ── Content provenance ────────────────────────────────────────────
    PUBLISHED_DATE = "published_date"  # ISO-8601 str or datetime; set by all channels
    SOURCE_ID = "source_id"  # internal source table ID (str)
    SOURCE_ENGINE = "source_engine"  # external engine name, e.g. "google_news"
    PLATFORM = "platform"  # social platform, e.g. "twitter"

    # ── Retrieval diagnostics ─────────────────────────────────────────
    MATCH_TYPE = "match_type"  # "title" | "content" (keyword channel)
    CHUNKS_USED = "chunks_used"  # int, chunk_vector only
    ENGAGEMENT = "engagement"  # int, social_media only
    IS_SCRAPING_REQUIRED = "is_scraping_required"  # bool, external channel


class ChannelType(str, Enum):
    """
    Identifies which channel produced a result.

    Priority is encoded in the enum value order — lower priority number
    means higher precedence during deduplication.
    """

    CHUNK_VECTOR = "chunk_vector"  # priority 1 — passage-level semantic
    ARTICLE_VECTOR = "article_vector"  # priority 2 — article-level semantic
    KEYWORD = "keyword"  # priority 3 — SQL LIKE search
    SOCIAL_MEDIA = "social_media"  # priority 5 — social posts
    EXTERNAL = "external"  # priority 10 — Google News / DDG


# Channel priority map — lower value = higher priority in dedup
CHANNEL_PRIORITY: dict[ChannelType, int] = {
    ChannelType.CHUNK_VECTOR: 1,
    ChannelType.ARTICLE_VECTOR: 2,
    ChannelType.KEYWORD: 3,
    ChannelType.SOCIAL_MEDIA: 5,
    ChannelType.EXTERNAL: 10,
}


@dataclass
class RetrievedChunk:
    """
    A single piece of retrieved content.

    Equivalent to CAgent's RetrievedChunk — the atomic unit that flows
    through the entire post-processing pipeline.
    """

    id: str  # article_id, post_id, or URL (unique key for dedup)
    content: str  # text content (passage, summary, or snippet)
    title: str = ""
    url: str = ""
    score: float = 0.0  # relevance score (updated by each post-processor)
    source_channel: ChannelType = ChannelType.CHUNK_VECTOR
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """Key used for deduplication — prefer URL, fall back to id."""
        return self.url if self.url else self.id


@dataclass
class ChannelResult:
    """
    Output from a single search channel.

    Equivalent to CAgent's SearchChannelResult — wraps the list of chunks
    with channel metadata for the post-processor pipeline.
    """

    channel_type: ChannelType
    chunks: list[RetrievedChunk]
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, channel_type: ChannelType) -> ChannelResult:
        """Convenience factory for failed/empty channel results."""
        return cls(channel_type=channel_type, chunks=[], latency_ms=0.0)


@dataclass
class SearchContext:
    """
    Shared context passed to all channels and post-processors.

    Equivalent to CAgent's SearchContext — carries the query, rewritten
    query, and configuration for the current retrieval request.
    """

    original_query: str
    rewritten_query: str = ""
    top_k: int = 10
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_query(self) -> str:
        """Use rewritten query if available, otherwise original."""
        return self.rewritten_query or self.original_query
