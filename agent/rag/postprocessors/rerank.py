"""
Rerank post-processor.

Order 10 — runs after RRF fusion, before quality filtering.

Equivalent to CAgent's RerankPostProcessor.

Uses a cross-encoder rerank model to re-score (query, document) pairs.
This is the most expensive post-processor but also the most accurate —
it considers the full interaction between query and document, not just
embedding similarity.

Supported providers:
  - Cohere Rerank API (rerank-v3.5)
  - Jina Rerank API
  - Disabled (passthrough) — if no API key is configured
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from rag.models import ChannelResult, RetrievedChunk, SearchContext
from rag.postprocessors.base import PostProcessor


class RerankProcessor(PostProcessor):
    """
    Cross-encoder reranking via external API.

    Falls back to passthrough (no-op) if:
    - No API key is configured
    - The rerank API call fails
    - There are fewer than 2 chunks to rerank
    """

    def __init__(
        self,
        *,
        provider: str = "cohere",       # "cohere" | "jina" | "disabled"
        api_key: Optional[str] = None,
        model: str = "rerank-v3.5",
        top_n: Optional[int] = None,    # None = use context.top_k
    ):
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._top_n = top_n

    def order(self) -> int:
        return 10

    def is_enabled(self, context: SearchContext) -> bool:
        if self._provider == "disabled":
            return False
        if not self._api_key:
            logger.debug("RerankProcessor disabled: no API key configured")
            return False
        return True

    def process(
        self,
        chunks: list[RetrievedChunk],
        all_results: list[ChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if len(chunks) < 2:
            return chunks

        top_n = self._top_n or context.top_k

        try:
            if self._provider == "cohere":
                return self._rerank_cohere(chunks, context.original_query, top_n)
            elif self._provider == "jina":
                return self._rerank_jina(chunks, context.original_query, top_n)
            else:
                logger.warning("Unknown rerank provider: {p}", p=self._provider)
                return chunks
        except Exception as e:
            # Rerank failure is non-fatal — fall back to current order
            logger.error("Rerank failed, passing through: {e}", e=e)
            return chunks

    # ── Cohere ────────────────────────────────────────────────────────

    def _rerank_cohere(
        self,
        chunks: list[RetrievedChunk],
        query: str,
        top_n: int,
    ) -> list[RetrievedChunk]:
        import cohere

        client = cohere.ClientV2(api_key=self._api_key)
        # Pass title alongside content so the cross-encoder sees the article
        # heading — titles often contain the clearest topical signal.
        documents = [
            {"text": c.content, "title": c.title} if c.title else c.content
            for c in chunks
        ]

        response = client.rerank(
            query=query,
            documents=documents,
            model=self._model,
            top_n=min(top_n, len(chunks)),
        )

        reranked = []
        for item in response.results:
            chunk = chunks[item.index]
            chunk.score = item.relevance_score
            reranked.append(chunk)

        logger.info(
            "Cohere rerank: {input} → {output} chunks",
            input=len(chunks),
            output=len(reranked),
        )
        return reranked

    # ── Jina ──────────────────────────────────────────────────────────

    def _rerank_jina(
        self,
        chunks: list[RetrievedChunk],
        query: str,
        top_n: int,
    ) -> list[RetrievedChunk]:
        import requests

        # Pass title alongside content — Jina supports {"text", "title"} dicts.
        documents = [
            {"text": c.content, "title": c.title} if c.title else c.content
            for c in chunks
        ]

        response = requests.post(
            "https://api.jina.ai/v1/rerank",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "documents": documents,
                "model": self._model if "jina" in self._model else "jina-reranker-v2-base-multilingual",
                "top_n": min(top_n, len(chunks)),
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        reranked = []
        for item in data.get("results", []):
            idx = item["index"]
            chunk = chunks[idx]
            chunk.score = item["relevance_score"]
            reranked.append(chunk)

        # Sort by score descending
        reranked.sort(key=lambda c: c.score, reverse=True)

        logger.info(
            "Jina rerank: {input} → {output} chunks",
            input=len(chunks),
            output=len(reranked),
        )
        return reranked
