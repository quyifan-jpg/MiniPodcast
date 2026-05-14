"""
External search channel — Google News + DuckDuckGo.

Priority 10 — lowest priority, broadest recall. Searches the public
internet for content not yet in the internal knowledge base.

Results from this channel have is_scrapping_required=True because they
are URLs only — full content must be scraped downstream.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext


class ExternalSearchChannel(SearchChannel):
    """
    Combines Google News and DuckDuckGo into a single channel.

    Both engines run in parallel, results are merged and deduplicated
    by URL before returning.
    """

    def __init__(
        self,
        *,
        google_news_max: int = 5,
        duckduckgo_max: int = 8,
    ):
        self._google_news_max = google_news_max
        self._duckduckgo_max = duckduckgo_max

    def channel_type(self) -> ChannelType:
        return ChannelType.EXTERNAL

    def priority(self) -> int:
        return 10

    async def search(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()
        query = context.effective_query

        # Run both external engines in parallel
        google_task = asyncio.to_thread(self._search_google_news, query)
        ddg_task = asyncio.to_thread(self._search_duckduckgo, query)

        google_results, ddg_results = await asyncio.gather(
            google_task, ddg_task, return_exceptions=True
        )

        # Handle exceptions gracefully
        if isinstance(google_results, Exception):
            logger.warning("Google News failed: {e}", e=google_results)
            google_results = []
        if isinstance(ddg_results, Exception):
            logger.warning("DuckDuckGo failed: {e}", e=ddg_results)
            ddg_results = []

        # Merge + deduplicate by URL
        seen_urls: set[str] = set()
        chunks: list[RetrievedChunk] = []

        # Google News results first (slightly higher quality for news)
        for chunk in google_results + ddg_results:
            if chunk.url and chunk.url not in seen_urls:
                seen_urls.add(chunk.url)
                chunks.append(chunk)

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "ExternalSearchChannel: {count} results in {ms:.0f}ms "
            "(google={g}, ddg={d})",
            count=len(chunks),
            ms=elapsed,
            g=len(google_results) if isinstance(google_results, list) else 0,
            d=len(ddg_results) if isinstance(ddg_results, list) else 0,
        )
        return ChannelResult(
            channel_type=ChannelType.EXTERNAL,
            chunks=chunks,
            latency_ms=elapsed,
        )

    # ── Google News ───────────────────────────────────────────────────

    def _search_google_news(self, query: str) -> list[RetrievedChunk]:
        try:
            from gnews import GNews

            google_news = GNews(
                language=None,
                country=None,
                period=None,
                max_results=self._google_news_max,
                exclude_websites=[],
            )
            results = google_news.get_news(query)
        except Exception as e:
            logger.warning("Google News search error: {e}", e=e)
            return []

        chunks = []
        for i, item in enumerate(results or []):
            url = item.get("url", "")
            title = item.get("title", "")
            description = item.get("description", "")
            published = item.get("published date", "")

            # Score by position (top results are more relevant)
            score = 1.0 - (i * 0.1)  # 1.0, 0.9, 0.8, ...
            score = max(score, 0.3)

            chunks.append(RetrievedChunk(
                id=f"gnews_{hash(url) % 10**8}",
                content=description,
                title=title,
                url=url,
                score=score,
                source_channel=ChannelType.EXTERNAL,
                metadata={
                    MetadataKey.PUBLISHED_DATE:      published,
                    MetadataKey.SOURCE_ENGINE:       "google_news",
                    MetadataKey.IS_SCRAPING_REQUIRED: True,
                },
            ))

        return chunks

    # ── DuckDuckGo ────────────────────────────────────────────────────

    def _search_duckduckgo(self, query: str) -> list[RetrievedChunk]:
        try:
            from duckduckgo_search import DDGS

            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=self._duckduckgo_max))
        except Exception as e:
            logger.warning("DuckDuckGo search error: {e}", e=e)
            return []

        chunks = []
        for i, item in enumerate(results or []):
            url = item.get("href", item.get("link", ""))
            title = item.get("title", "")
            body = item.get("body", item.get("snippet", ""))

            score = 1.0 - (i * 0.08)
            score = max(score, 0.3)

            chunks.append(RetrievedChunk(
                id=f"ddg_{hash(url) % 10**8}",
                content=body,
                title=title,
                url=url,
                score=score,
                source_channel=ChannelType.EXTERNAL,
                metadata={
                    MetadataKey.SOURCE_ENGINE:       "duckduckgo",
                    MetadataKey.IS_SCRAPING_REQUIRED: True,
                },
            ))

        return chunks
