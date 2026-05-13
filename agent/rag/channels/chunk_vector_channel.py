"""
Chunk-level vector search channel.

Priority 1 — highest precision. Searches the Milvus `chunk_vectors`
collection to find the most relevant *passages* (not whole articles).
This is the primary retrieval channel for podcast script generation.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, RetrievedChunk, SearchContext


# ── Defaults ──────────────────────────────────────────────────────────
EMBEDDING_MODEL        = "text-embedding-3-small"
MIN_SIMILARITY         = 0.22   # IP score threshold; post-processors filter further
MAX_CHUNKS_PER_ARTICLE = 3


class ChunkVectorChannel(SearchChannel):
    """
    Milvus chunk-level semantic search.

    Reads from the `chunk_vectors` collection populated by
    processors/chunk_processor.py. Returns passages grouped by article,
    with best-similarity as the score.
    """

    def __init__(
        self,
        *,
        top_k_multiplier: int = 2,
        min_similarity: float = MIN_SIMILARITY,
        max_chunks_per_article: int = MAX_CHUNKS_PER_ARTICLE,
    ):
        self._top_k_multiplier = top_k_multiplier
        self._min_similarity = min_similarity
        self._max_chunks_per_article = max_chunks_per_article

    def channel_type(self) -> ChannelType:
        return ChannelType.CHUNK_VECTOR

    def priority(self) -> int:
        return 1

    def is_enabled(self, context: SearchContext) -> bool:
        try:
            from db.milvus import get_milvus
            get_milvus()
            return True
        except Exception as e:
            logger.warning("ChunkVectorChannel disabled: Milvus unavailable ({e})", e=e)
            return False

    async def search(self, context: SearchContext) -> ChannelResult:
        # Milvus and OpenAI calls are blocking — run in thread pool
        return await asyncio.to_thread(self._search_sync, context)

    def _search_sync(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()
        query = context.effective_query
        top_k = context.top_k * self._top_k_multiplier

        # 1. Generate query embedding
        query_embedding = self._generate_embedding(query)
        if query_embedding is None:
            logger.error("ChunkVectorChannel: failed to generate embedding")
            return ChannelResult.empty(ChannelType.CHUNK_VECTOR)

        # 2. Search Milvus
        from db.milvus import get_milvus

        try:
            mv   = get_milvus()
            hits = mv.search_chunks(query_embedding, top_k=top_k)
        except Exception as e:
            logger.error("Milvus chunk search failed: {e}", e=e)
            return ChannelResult.empty(ChannelType.CHUNK_VECTOR)

        # 3. Filter by threshold
        hits = [h for h in hits if h["score"] >= self._min_similarity]
        if not hits:
            return ChannelResult.empty(ChannelType.CHUNK_VECTOR)

        # 4. Fetch article metadata (published_date, source_id) from MySQL
        from db.config import get_tracking_db_path

        db_path     = get_tracking_db_path()
        article_ids = list({h["article_id"] for h in hits})
        article_map = self._fetch_articles(db_path, article_ids)

        # 5. Group by article, build RetrievedChunk list
        chunks = self._group_by_article(hits, article_map)

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "ChunkVectorChannel: {count} results in {ms:.0f}ms",
            count=len(chunks),
            ms=elapsed,
        )
        return ChannelResult(
            channel_type=ChannelType.CHUNK_VECTOR,
            chunks=chunks,
            latency_ms=elapsed,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _generate_embedding(self, query: str) -> list[float] | None:
        from openai import OpenAI
        from utils.load_api_keys import load_api_key

        api_key = load_api_key("OPENAI_API_KEY")
        if not api_key:
            return None
        try:
            client = OpenAI(api_key=api_key)
            resp = client.embeddings.create(input=query, model=EMBEDDING_MODEL)
            return resp.data[0].embedding
        except Exception as e:
            logger.error("Embedding generation failed: {e}", e=e)
            return None

    def _fetch_articles(self, db_path: str, article_ids: list[int]) -> dict[int, dict]:
        if not article_ids:
            return {}
        from db.connection import execute_query

        placeholders = ",".join(["%s"] * len(article_ids))
        rows = execute_query(
            db_path,
            f"SELECT id, title, url, published_date, source_id "
            f"FROM crawled_articles WHERE id IN ({placeholders})",
            article_ids,
            fetch=True,
        )
        return {row["id"]: row for row in rows} if rows else {}

    def _group_by_article(
        self,
        hits: list[dict],
        article_map: dict[int, dict],
    ) -> list[RetrievedChunk]:
        """Group hits by article_id, keep top N chunks per article, return one RetrievedChunk per article."""
        by_article: dict[int, list[dict]] = {}
        for hit in hits:
            aid = hit["article_id"]
            if aid not in by_article:
                by_article[aid] = []
            if len(by_article[aid]) < self._max_chunks_per_article:
                by_article[aid].append(hit)

        results = []
        for article_id, chunk_hits in by_article.items():
            chunk_hits.sort(key=lambda h: h["chunk_index"])
            best_score = max(h["score"] for h in chunk_hits)
            passages   = "\n\n---\n\n".join(h["chunk_text"] for h in chunk_hits)

            # Milvus hit carries title+url; MySQL map carries published_date+source_id
            article_meta = article_map.get(article_id, {})
            title        = chunk_hits[0].get("title") or article_meta.get("title", "Untitled")
            url          = chunk_hits[0].get("url")   or article_meta.get("url", "")

            results.append(RetrievedChunk(
                id=str(article_id),
                content=passages,
                title=title,
                url=url,
                score=float(best_score),
                source_channel=ChannelType.CHUNK_VECTOR,
                metadata={
                    "published_date": str(article_meta.get("published_date", "")),
                    "source_id":      str(article_meta.get("source_id", "")),
                    "chunks_used":    len(chunk_hits),
                },
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results
