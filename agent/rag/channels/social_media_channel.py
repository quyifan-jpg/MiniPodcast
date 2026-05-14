"""
Social media search channel.

Priority 5 — supplements article content with social signals (tweets,
posts). Useful for trending topics and real-time reactions that haven't
been published as articles yet.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext


class SocialMediaChannel(SearchChannel):
    """Keyword search on the social_media posts table."""

    def __init__(self, *, days_back: int = 7):
        self._days_back = days_back

    def channel_type(self) -> ChannelType:
        return ChannelType.SOCIAL_MEDIA

    def priority(self) -> int:
        return 5

    def is_enabled(self, context: SearchContext) -> bool:
        from db.config import get_social_media_db_path

        db_path = get_social_media_db_path()
        exists = os.path.exists(db_path)
        if not exists:
            logger.debug("SocialMediaChannel disabled: DB not found at {p}", p=db_path)
        return exists

    async def search(self, context: SearchContext) -> ChannelResult:
        return await asyncio.to_thread(self._search_sync, context)

    def _search_sync(self, context: SearchContext) -> ChannelResult:
        start = time.perf_counter()
        query = context.original_query
        top_k = context.top_k

        from db.config import get_social_media_db_path
        from db.connection import execute_query

        db_path = get_social_media_db_path()
        date_from = (datetime.now() - timedelta(days=self._days_back)).isoformat()
        search_term = f"%{query}%"

        try:
            rows = execute_query(
                db_path,
                """
                SELECT
                    post_id,
                    user_display_name,
                    post_timestamp,
                    post_url,
                    post_text,
                    platform,
                    COALESCE(engagement_like_count, 0)
                      + COALESCE(engagement_retweet_count, 0)
                      + COALESCE(engagement_reply_count, 0) AS engagement
                FROM posts
                WHERE post_timestamp >= %s
                  AND (post_text LIKE %s OR user_display_name LIKE %s)
                ORDER BY engagement DESC, post_timestamp DESC
                LIMIT %s
                """,
                (date_from, search_term, search_term, top_k),
                fetch=True,
            )
        except Exception as e:
            logger.error("SocialMediaChannel query failed: {e}", e=e)
            return ChannelResult.empty(ChannelType.SOCIAL_MEDIA)

        if not rows:
            elapsed = (time.perf_counter() - start) * 1000
            return ChannelResult(channel_type=ChannelType.SOCIAL_MEDIA, chunks=[], latency_ms=elapsed)

        # Normalize engagement to a 0-1 score
        max_engagement = max(row.get("engagement", 0) for row in rows) or 1

        chunks = []
        for row in rows:
            engagement = row.get("engagement", 0)
            score = engagement / max_engagement if max_engagement > 0 else 0.5
            post_text = row.get("post_text", "")
            platform = row.get("platform", "social")

            chunks.append(
                RetrievedChunk(
                    id=f"social_{row['post_id']}",
                    content=post_text[:500] if len(post_text) > 500 else post_text,
                    title=f"@{row.get('user_display_name', 'unknown')} on {platform.title()}",
                    url=row.get("post_url", ""),
                    score=score,
                    source_channel=ChannelType.SOCIAL_MEDIA,
                    metadata={
                        MetadataKey.PUBLISHED_DATE: row.get("post_timestamp", ""),
                        MetadataKey.PLATFORM: platform,
                        MetadataKey.ENGAGEMENT: engagement,
                    },
                )
            )

        elapsed = (time.perf_counter() - start) * 1000
        logger.info(
            "SocialMediaChannel: {count} results in {ms:.0f}ms",
            count=len(chunks),
            ms=elapsed,
        )
        return ChannelResult(
            channel_type=ChannelType.SOCIAL_MEDIA,
            chunks=chunks,
            latency_ms=elapsed,
        )
