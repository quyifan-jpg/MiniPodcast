"""
Factory for assembling the multi-channel retrieval engine.

This is the single place where channels and post-processors are wired
together. To add a new channel or processor:
  1. Implement the interface (SearchChannel or PostProcessor)
  2. Add it to the lists below
  3. Done

Equivalent to CAgent's Spring @Autowired List<SearchChannel> injection,
but explicit (Python doesn't have a DI container).
"""

from __future__ import annotations

from loguru import logger

from rag.config import rag_settings
from rag.engine import MultiChannelRetrievalEngine


def create_retrieval_engine() -> MultiChannelRetrievalEngine:
    """
    Build and return the retrieval engine with all configured
    channels and post-processors.

    Respects rag_settings toggles — disabled channels/processors
    are not even instantiated.
    """
    channels = _build_channels()
    postprocessors = _build_postprocessors()

    logger.info(
        "RAG engine created: {nc} channel(s), {np} post-processor(s)",
        nc=len(channels),
        np=len(postprocessors),
    )

    return MultiChannelRetrievalEngine(
        channels=channels,
        postprocessors=postprocessors,
        channel_timeout_s=rag_settings.channel_timeout_s,
    )


def _build_channels():
    """Instantiate enabled channels based on config."""
    from rag.channels.chunk_vector_channel import ChunkVectorChannel
    from rag.channels.article_vector_channel import ArticleVectorChannel
    from rag.channels.keyword_channel import KeywordChannel
    from rag.channels.social_media_channel import SocialMediaChannel
    from rag.channels.external_search_channel import ExternalSearchChannel

    channels = []

    if rag_settings.chunk_vector_enabled:
        channels.append(
            ChunkVectorChannel(
                top_k_multiplier=rag_settings.top_k_multiplier,
                min_similarity=rag_settings.chunk_min_similarity,
                max_chunks_per_article=rag_settings.chunk_max_per_article,
            )
        )

    if rag_settings.article_vector_enabled:
        channels.append(
            ArticleVectorChannel(
                top_k_multiplier=rag_settings.top_k_multiplier,
                min_similarity=rag_settings.article_min_similarity,
            )
        )

    if rag_settings.keyword_enabled:
        channels.append(
            KeywordChannel(
                top_k_multiplier=rag_settings.top_k_multiplier,
            )
        )

    if rag_settings.social_media_enabled:
        channels.append(
            SocialMediaChannel(
                days_back=rag_settings.social_media_days_back,
            )
        )

    if rag_settings.external_search_enabled:
        channels.append(
            ExternalSearchChannel(
                google_news_max=rag_settings.google_news_max,
                duckduckgo_max=rag_settings.duckduckgo_max,
            )
        )

    return channels


def _build_postprocessors():
    """Instantiate enabled post-processors based on config."""
    from rag.postprocessors.dedup import DeduplicationProcessor
    from rag.postprocessors.rrf import RRFProcessor
    from rag.postprocessors.rerank import RerankProcessor
    from rag.postprocessors.quality_filter import QualityFilterProcessor

    processors = []

    if rag_settings.dedup_enabled:
        processors.append(DeduplicationProcessor())

    if rag_settings.rrf_enabled:
        processors.append(RRFProcessor(k=rag_settings.rrf_k))

    if rag_settings.rerank_enabled:
        processors.append(
            RerankProcessor(
                provider=rag_settings.rerank_provider,
                api_key=rag_settings.rerank_api_key,
                model=rag_settings.rerank_model,
            )
        )

    if rag_settings.freshness_enabled:
        from rag.postprocessors.freshness import FreshnessBoostProcessor

        processors.append(
            FreshnessBoostProcessor(
                half_life_days=rag_settings.freshness_half_life_days,
                floor_multiplier=rag_settings.freshness_floor_multiplier,
            )
        )

    if rag_settings.quality_filter_enabled:
        processors.append(
            QualityFilterProcessor(
                min_score=rag_settings.quality_min_score,
                min_content_length=rag_settings.quality_min_length,
                keep_best_when_all_filtered=rag_settings.quality_keep_best,
                max_age_days=rag_settings.quality_max_age_days,
            )
        )

    return processors


# ── Module-level singleton ────────────────────────────────────────────
# Lazy initialization to avoid import-time side effects (FAISS loading etc.)
_engine: MultiChannelRetrievalEngine | None = None


def get_retrieval_engine() -> MultiChannelRetrievalEngine:
    """Get or create the singleton retrieval engine."""
    global _engine
    if _engine is None:
        _engine = create_retrieval_engine()
    return _engine
