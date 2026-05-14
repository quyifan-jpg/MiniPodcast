"""
Chunk-level vector search channel.

Priority 1 — highest precision. Searches the FAISS chunk index to find
the most relevant *passages* (not whole articles). This is the primary
retrieval channel for podcast script generation.

Wraps the existing logic in tools/chunk_search.py, extracting the pure
retrieval functions without the Agno agent dependency.
"""

from __future__ import annotations

import asyncio
import os
import time

import numpy as np
from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext


# ── Defaults ──────────────────────────────────────────────────────────
EMBEDDING_MODEL = "text-embedding-3-small"
MIN_SIMILARITY = 0.55          # slightly lower than the tool default (0.60)
                                # because post-processors will filter further
MAX_CHUNKS_PER_ARTICLE = 3


class ChunkVectorChannel(SearchChannel):
    """
    FAISS chunk-level semantic search.

    Reuses the existing FAISS index built by processors/chunk_processor.py.
    Returns passages grouped by article, with best-similarity as the score.
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
        from db.config import get_chunk_faiss_db_path

        index_path, mapping_path = get_chunk_faiss_db_path()
        exists = os.path.exists(index_path) and os.path.exists(mapping_path)
        if not exists:
            logger.warning("ChunkVectorChannel disabled: FAISS index not found")
        return exists

    async def search(self, context: SearchContext) -> ChannelResult:
        # FAISS and OpenAI calls are blocking — run in thread pool
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

        # 2. Search FAISS
        from db.config import get_chunk_faiss_db_path, get_tracking_db_path

        index_path, mapping_path = get_chunk_faiss_db_path()
        scored_chunks = self._search_faiss(query_embedding, index_path, mapping_path, top_k)
        if not scored_chunks:
            return ChannelResult.empty(ChannelType.CHUNK_VECTOR)

        # 3. Fetch chunk texts and article metadata from DB
        db_path = get_tracking_db_path()
        chunk_ids = [cid for cid, _ in scored_chunks]
        chunk_map = self._fetch_chunks(db_path, chunk_ids)
        article_ids = list({c["article_id"] for c in chunk_map.values()})
        article_map = self._fetch_articles(db_path, article_ids)

        # 4. Group by article, build RetrievedChunk list
        chunks = self._group_by_article(scored_chunks, chunk_map, article_map)

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

    def _fetch_chunks(self, db_path: str, chunk_ids: list[int]) -> dict[int, dict]:
        if not chunk_ids:
            return {}
        from db.connection import execute_query

        placeholders = ",".join(["%s"] * len(chunk_ids))
        rows = execute_query(
            db_path,
            f"SELECT id, article_id, chunk_index, chunk_text "
            f"FROM article_chunks WHERE id IN ({placeholders})",
            chunk_ids,
            fetch=True,
        )
        return {row["id"]: row for row in rows} if rows else {}

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
        scored_chunks: list[tuple[int, float]],
        chunk_map: dict[int, dict],
        article_map: dict[int, dict],
    ) -> list[RetrievedChunk]:
        """Group chunks by article, keep top N per article, return one RetrievedChunk per article."""
        by_article: dict[int, list[tuple[dict, float]]] = {}
        for chunk_id, sim in scored_chunks:
            chunk = chunk_map.get(chunk_id)
            if not chunk:
                continue
            aid = chunk["article_id"]
            if aid not in by_article:
                by_article[aid] = []
            if len(by_article[aid]) < self._max_chunks_per_article:
                by_article[aid].append((chunk, sim))

        results = []
        for article_id, chunk_sims in by_article.items():
            article = article_map.get(article_id)
            if not article:
                continue

            chunk_sims.sort(key=lambda x: x[0]["chunk_index"])
            best_sim = max(s for _, s in chunk_sims)
            passages = "\n\n---\n\n".join(c["chunk_text"] for c, _ in chunk_sims)

            results.append(RetrievedChunk(
                id=str(article_id),
                content=passages,
                title=article.get("title", "Untitled"),
                url=article.get("url", ""),
                score=best_sim,
                source_channel=ChannelType.CHUNK_VECTOR,
                metadata={
                    MetadataKey.PUBLISHED_DATE: article.get("published_date", ""),
                    MetadataKey.SOURCE_ID:      str(article.get("source_id", "")),
                    MetadataKey.CHUNKS_USED:    len(chunk_sims),
                },
            ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results
