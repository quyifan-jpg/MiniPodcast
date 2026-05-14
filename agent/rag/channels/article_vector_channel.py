"""
Article-level vector search channel.

Priority 2 — broader recall than chunk search. Searches the FAISS article
index using full-article embeddings (title + summary + content combined).

Complements ChunkVectorChannel: chunk search is precise (finds specific
passages), article search casts a wider net (finds thematically related
articles even if no single passage is a strong match).
"""

from __future__ import annotations

import asyncio
import os
import time

import numpy as np
from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext

EMBEDDING_MODEL = "text-embedding-3-small"
MIN_SIMILARITY = 0.70  # lower than original 0.85 — post-processors will filter


class ArticleVectorChannel(SearchChannel):
    """FAISS article-level semantic search."""

    def __init__(self, *, top_k_multiplier: int = 2, min_similarity: float = MIN_SIMILARITY):
        self._top_k_multiplier = top_k_multiplier
        self._min_similarity = min_similarity

    def channel_type(self) -> ChannelType:
        return ChannelType.ARTICLE_VECTOR

    def priority(self) -> int:
        return 2

    def is_enabled(self, context: SearchContext) -> bool:
        from db.config import get_faiss_db_path

        index_path, mapping_path = get_faiss_db_path()
        exists = os.path.exists(index_path) and os.path.exists(mapping_path)
        if not exists:
            logger.warning("ArticleVectorChannel disabled: FAISS index not found")
        return exists

    async def search(self, context: SearchContext) -> ChannelResult:
        return await asyncio.to_thread(self._search_sync, context)

    def _search_sync(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()
        query = context.effective_query
        top_k = context.top_k * self._top_k_multiplier

        # 1. Generate embedding
        query_embedding = self._generate_embedding(query)
        if query_embedding is None:
            return ChannelResult.empty(ChannelType.ARTICLE_VECTOR)

        # 2. Search FAISS
        from db.config import get_faiss_db_path, get_tracking_db_path

        index_path, mapping_path = get_faiss_db_path()
        scored_articles = self._search_faiss(query_embedding, index_path, mapping_path, top_k)
        if not scored_articles:
            return ChannelResult.empty(ChannelType.ARTICLE_VECTOR)

        # 3. Fetch article details
        db_path = get_tracking_db_path()
        article_ids = [aid for aid, _ in scored_articles]
        sim_map = {aid: sim for aid, sim in scored_articles}
        articles = self._fetch_articles(db_path, article_ids)

        # 4. Build results
        chunks = []
        for article in articles:
            aid = article["id"]
            sim = sim_map.get(aid, 0.0)
            chunks.append(RetrievedChunk(
                id=str(aid),
                content=article.get("summary") or (article.get("content", "")[:500]),
                title=article.get("title", "Untitled"),
                url=article.get("url", ""),
                score=sim,
                source_channel=ChannelType.ARTICLE_VECTOR,
                metadata={
                    MetadataKey.PUBLISHED_DATE: article.get("published_date", ""),
                    MetadataKey.SOURCE_ID:      str(article.get("source_id", "")),
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

    def _search_faiss(
        self,
        query_embedding: list[float],
        index_path: str,
        mapping_path: str,
        top_k: int,
    ) -> list[tuple[int, float]]:
        import faiss

        try:
            index = faiss.read_index(index_path)
            id_map = np.load(mapping_path).tolist()
        except Exception as e:
            logger.error("FAISS load error: {e}", e=e)
            return []

        query_vec = np.array([query_embedding], dtype=np.float32)
        distances, indices = index.search(query_vec, top_k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(id_map):
                continue
            similarity = float(np.exp(-dist)) if dist > 0 else 1.0
            if similarity >= self._min_similarity:
                results.append((id_map[idx], similarity))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def _fetch_articles(self, db_path: str, article_ids: list[int]) -> list[dict]:
        if not article_ids:
            return []
        from db.connection import execute_query

        placeholders = ",".join(["%s"] * len(article_ids))
        rows = execute_query(
            db_path,
            f"SELECT id, title, url, published_date, summary, content, source_id "
            f"FROM crawled_articles WHERE id IN ({placeholders})",
            article_ids,
            fetch=True,
        )
        return rows or []
