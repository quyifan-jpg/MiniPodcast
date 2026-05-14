"""
Keyword search channel.

Priority 3 — complements vector search by catching exact matches that
embedding models might miss (e.g., product names, version numbers,
acronyms like "GPT-4o" or "K8s").

Uses SQL LIKE on crawled_articles. Future improvement: MySQL FULLTEXT index.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext


class KeywordChannel(SearchChannel):
    """MySQL keyword search on crawled_articles."""

    def __init__(self, *, top_k_multiplier: int = 2):
        self._top_k_multiplier = top_k_multiplier

    def channel_type(self) -> ChannelType:
        return ChannelType.KEYWORD

    def priority(self) -> int:
        return 3

    async def search(self, context: SearchContext) -> ChannelResult:
        return await asyncio.to_thread(self._search_sync, context)

    def _search_sync(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()

        # Use original query for keyword search — rewritten queries add
        # synonyms that are good for vector but bad for LIKE matching
        query = context.original_query
        top_k = context.top_k * self._top_k_multiplier

        from db.config import get_tracking_db_path
        from db.connection import execute_query

        db_path = get_tracking_db_path()
        like_term = f"%{query}%"

        try:
            rows = execute_query(
                db_path,
                """
                SELECT DISTINCT ca.id, ca.title, ca.url, ca.published_date,
                       COALESCE(ca.summary, SUBSTRING(ca.content, 1, 500)) AS content,
                       ca.source_id
                FROM crawled_articles ca
                WHERE ca.processed = 1
                  AND (ca.title LIKE %s OR ca.content LIKE %s OR ca.summary LIKE %s)
                ORDER BY ca.published_date DESC
                LIMIT %s
                """,
                (like_term, like_term, like_term, top_k),
                fetch=True,
            )
        except Exception as e:
            logger.error("KeywordChannel query failed: {e}", e=e)
            return ChannelResult.empty(ChannelType.KEYWORD)

        if not rows:
            elapsed = (time.perf_counter() - start) * 1000
            return ChannelResult(
                channel_type=ChannelType.KEYWORD,
                chunks=[],
                latency_ms=elapsed,
            )

        # Keyword matches don't have a natural similarity score.
        # Assign 1.0 for title matches, 0.8 for content-only matches.
        query_lower = query.lower()
        chunks = []
        for row in rows:
            title = row.get("title", "")
            title_match = query_lower in title.lower() if title else False
            score = 1.0 if title_match else 0.8

            chunks.append(
                RetrievedChunk(
                    id=str(row["id"]),
                    content=row.get("content", ""),
                    title=title,
                    url=row.get("url", ""),
                    score=score,
                    source_channel=ChannelType.KEYWORD,
                    metadata={
                        MetadataKey.PUBLISHED_DATE: row.get("published_date", ""),
                        MetadataKey.SOURCE_ID: str(row.get("source_id", "")),
                        MetadataKey.MATCH_TYPE: "title" if title_match else "content",
                    },
                )
            )

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "KeywordChannel: {count} results in {ms:.0f}ms",
            count=len(chunks),
            ms=elapsed,
        )
        return ChannelResult(
            channel_type=ChannelType.KEYWORD,
            chunks=chunks,
            latency_ms=elapsed,
        )
