"""
Freshness boost post-processor.

Order 15 — runs after Rerank (10), before QualityFilter (20).

Applies an exponential time-decay multiplier to each chunk's score based
on how old the article is.  Newer content scores higher; content older
than `max_age_days` is capped at a floor multiplier rather than zeroed
(hard filtering is left to QualityFilterProcessor).

Decay formula:
    multiplier = exp(-λ × days_old)    where λ = ln(2) / half_life_days

Example with half_life_days=30:
    0 days old  → multiplier = 1.00   (no penalty)
    30 days old → multiplier = 0.50   (score halved)
    90 days old → multiplier = 0.125  (score quartered)
   180 days old → multiplier = 0.016

The multiplier is clamped to [floor_multiplier, 1.0] so very old but
highly relevant content is never completely suppressed.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from rag.models import ChannelResult, MetadataKey, RetrievedChunk, SearchContext
from rag.postprocessors.base import PostProcessor

_PARSE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _parse_date(raw: object) -> Optional[datetime]:
    """Parse a published_date value into an aware datetime (UTC)."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    for fmt in _PARSE_FORMATS:
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class FreshnessBoostProcessor(PostProcessor):
    """
    Exponential time-decay score multiplier.

    Args:
        half_life_days:   Days until a chunk's freshness multiplier halves.
                          Lower → stronger recency bias. Default 30 days.
        floor_multiplier: Minimum multiplier applied even to very old content.
                          Prevents total suppression. Default 0.05.
        enabled:          Quick toggle without changing factory config.
    """

    def __init__(
        self,
        *,
        half_life_days: float = 30.0,
        floor_multiplier: float = 0.05,
        enabled: bool = True,
    ):
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self._lambda = math.log(2) / half_life_days   # decay constant
        self._floor = max(0.0, min(floor_multiplier, 1.0))
        self._enabled_flag = enabled

    def order(self) -> int:
        return 15

    def is_enabled(self, context: SearchContext) -> bool:
        return self._enabled_flag

    def process(
        self,
        chunks: list[RetrievedChunk],
        all_results: list[ChannelResult],
        context: SearchContext,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        now = datetime.now(timezone.utc)
        boosted = 0
        skipped_no_date = 0

        for chunk in chunks:
            raw_date = chunk.metadata.get(MetadataKey.PUBLISHED_DATE)
            pub_dt = _parse_date(raw_date)

            if pub_dt is None:
                # No date → treat as neutral (multiplier = 1.0, no change)
                skipped_no_date += 1
                continue

            days_old = max(0.0, (now - pub_dt).total_seconds() / 86_400)
            multiplier = math.exp(-self._lambda * days_old)
            multiplier = max(self._floor, multiplier)

            chunk.score *= multiplier
            boosted += 1

        logger.debug(
            "FreshnessBoost: adjusted {b}/{t} chunks "
            "({s} had no date, half_life={hl}d)",
            b=boosted,
            t=len(chunks),
            s=skipped_no_date,
            hl=round(math.log(2) / self._lambda, 1),
        )

        # Re-sort by updated scores
        return sorted(chunks, key=lambda c: c.score, reverse=True)
