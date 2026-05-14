"""
Quality filter post-processor.

Order 20 — runs last. Filters out low-quality results based on:
- Minimum relevance score
- Minimum content length

Equivalent to CAgent's QualityFilterPostProcessor.

Safety: if all chunks are filtered out, keeps the single best chunk
to avoid returning empty results (configurable).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from rag.models import ChannelResult, MetadataKey, RetrievedChunk, SearchContext
from rag.postprocessors.base import PostProcessor
from rag.postprocessors.freshness import _parse_date


class QualityFilterProcessor(PostProcessor):
    """Score, length, and staleness quality gate."""

    def __init__(
        self,
        *,
        min_score: float = 0.3,
        min_content_length: int = 10,
        keep_best_when_all_filtered: bool = True,
        max_age_days: Optional[float] = None,
    ):
        """
        Args:
            min_score: Chunks with score below this are dropped.
            min_content_length: Chunks with content shorter than this
                                (in characters) are dropped.
            keep_best_when_all_filtered: If True and ALL chunks are
                                         filtered out, keep the one
                                         with the highest score.
            max_age_days: If set, chunks older than this many days are
                          dropped before the score/length check. Chunks
                          with no published_date are kept (safe default).
        """
        self._min_score = min_score
        self._min_content_length = min_content_length
        self._keep_best = keep_best_when_all_filtered
        self._max_age_days = max_age_days

    def order(self) -> int:
        return 20

    def process(
        self,
        chunks: list[RetrievedChunk],
        all_results: list[ChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        now = datetime.now(timezone.utc)

        # ── Step 1: hard freshness cutoff ─────────────────────────────
        # Stale content is unconditionally rejected — keep_best cannot
        # rescue it. Chunks with no published_date are kept (safe default).
        if self._max_age_days is not None:
            fresh, stale = [], []
            for c in chunks:
                raw = c.metadata.get(MetadataKey.PUBLISHED_DATE)
                pub_dt = _parse_date(raw)
                if pub_dt is None:
                    fresh.append(c)
                elif (now - pub_dt).total_seconds() / 86_400 <= self._max_age_days:
                    fresh.append(c)
                else:
                    stale.append(c)

            if stale:
                logger.info(
                    "QualityFilter: dropped {n} stale chunks (age > {a} days)",
                    n=len(stale),
                    a=self._max_age_days,
                )

            if not fresh:
                # All content was stale — return empty, no keep_best rescue.
                return []
        else:
            fresh = chunks

        # ── Step 2: score + length filter (with keep_best rescue) ─────
        filtered = [
            c for c in fresh
            if c.score >= self._min_score
            and len(c.content) >= self._min_content_length
        ]

        dropped = len(fresh) - len(filtered)
        if dropped > 0:
            logger.info(
                "QualityFilter: dropped {n} chunks (score < {s} or length < {l})",
                n=dropped,
                s=self._min_score,
                l=self._min_content_length,
            )

        if not filtered and self._keep_best:
            # Score/length filtered everything — return best fresh chunk as safety net.
            best = max(fresh, key=lambda c: c.score)
            logger.warning(
                "QualityFilter: all chunks filtered, keeping best "
                "(score={s:.3f}, id={id})",
                s=best.score,
                id=best.id,
            )
            return [best]

        return filtered[: context.top_k]
