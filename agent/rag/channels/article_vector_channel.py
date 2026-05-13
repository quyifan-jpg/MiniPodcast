"""
Article-level vector search channel.

Priority 2 — broader recall than chunk search. Searches the Milvus
`article_vectors` collection using full-article embeddings.

Complements ChunkVectorChannel: chunk search is precise (finds specific
passages), article search casts a wider net (finds thematically related
articles even if no single passage is a strong match).
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, RetrievedChunk, SearchContext

EMBEDDING_MODEL = "text-embedding-3-small"
MIN_SIMILARITY  = 0.35   # IP score threshold; post-processors filter further


class ArticleVectorChannel(SearchChannel):
    """Milvus article-level semantic search."""

    def __init__(self, *, top_k_multiplier: int = 2, min_similarity: float = MIN_SIMILARITY):
        self._top_k_multiplier = top_k_multiplier
        self._min_similarity = min_similarity

    def channel_type(self) -> ChannelType:
        return ChannelType.ARTICLE_VECTOR

    def priority(self) -> int:
        return 2

    def is_enabled(self, context: SearchContext) -> bool:
        try:
            from db.milvus import get_milvus
            get_milvus()
            return True
        except Exception as e:
            logger.warning("ArticleVectorChannel disabled: Milvus unavailable ({e})", e=e)
            return False

    async def search(self, context: SearchContext) -> ChannelResult:
        return await asyncio.to_thread(self._search_sync, context)

    def _search_sync(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()
        query = context.effective_query
        top_k = context.top_k * self._top_k_multiplier

        # 1. Generate query embedding
        query_embedding = self._generate_embedding(query)
        if query_embedding is None:
            return ChannelResult.empty(ChannelType.ARTICLE_VECTOR)

        # 2. Search Milvus
        from db.milvus import get_milvus

        try:
            mv   = get_milvus()
            hits = mv.search_articles(query_embedding, top_k=top_k)
        except Exception as e:
            logger.error("Milvus article search failed: {e}", e=e)
            return ChannelResult.empty(ChannelType.ARTICLE_VECTOR)

        # 3. Filter by threshold
        hits = [h for h in hits if h["score"] >= self._min_similarity]
        if not hits:
            return ChannelResult.empty(ChannelType.ARTICLE_VECTOR)

        # 4. Enrich with MySQL metadata (published_date, source_id, content for longer context)
        from db.config import get_tracking_db_path

        article_ids = [h["article_id"] for h in hits]
        article_map = self._fetch_articles(get_tracking_db_path(), article_ids)

        # 5. Build results
        chunks = []
        for h in hits:
            aid     = h["article_id"]
            article = article_map.get(aid, {})
            summary = h.get("summary") or article.get("summary") or (article.get("content", "") or "")[:500]
            chunks.append(RetrievedChunk(
                id=str(aid),
                content=summary,
                title=h.get("title") or article.get("title", "Untitled"),
                url=h.get("url") or article.get("url", ""),
                score=float(h["score"]),
                source_channel=ChannelType.ARTICLE_VECTOR,
                metadata={
                    "published_date": str(article.get("published_date", "")),
                    "source_id":      str(article.get("source_id", "")),
                },
            ))

        chunks.sort(key=lambda c: c.score, reverse=True)

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "ArticleVectorChannel: {count} results in {ms:.0f}ms",
            count=len(chunks),
            ms=elapsed,
        )
        return ChannelResult(
            channel_type=ChannelType.ARTICLE_VECTOR,
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
            f"SELECT id, title, url, published_date, summary, content, source_id "
            f"FROM crawled_articles WHERE id IN ({placeholders})",
            article_ids,
            fetch=True,
        )
        return {row["id"]: row for row in rows} if rows else {}
