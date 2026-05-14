"""
Tests for the metadata pipeline improvements:
  - MetadataKey constants (schema consistency)
  - FreshnessBoostProcessor (time-decay scoring)
  - QualityFilterProcessor.max_age_days (hard staleness cutoff)

Run:
    cd agent && python -m pytest tests/test_metadata_pipeline.py -v
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from rag.models import ChannelResult, ChannelType, MetadataKey, RetrievedChunk, SearchContext
from rag.postprocessors.freshness import FreshnessBoostProcessor, _parse_date
from rag.postprocessors.quality_filter import QualityFilterProcessor


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ctx(query: str = "test", top_k: int = 10) -> SearchContext:
    return SearchContext(original_query=query, top_k=top_k)


def _chunk(
    id: str,
    score: float = 0.8,
    published_date: str | datetime | None = None,
    content: str = "sufficient content here",
    title: str = "Test Title",
) -> RetrievedChunk:
    metadata = {}
    if published_date is not None:
        metadata[MetadataKey.PUBLISHED_DATE] = published_date
    return RetrievedChunk(
        id=id,
        content=content,
        title=title,
        url=f"https://example.com/{id}",
        score=score,
        source_channel=ChannelType.CHUNK_VECTOR,
        metadata=metadata,
    )


def _days_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


# ─── MetadataKey ──────────────────────────────────────────────────────────────


class TestMetadataKey:
    def test_constants_are_strings(self):
        assert isinstance(MetadataKey.PUBLISHED_DATE, str)
        assert isinstance(MetadataKey.SOURCE_ID, str)
        assert isinstance(MetadataKey.MATCH_TYPE, str)
        assert isinstance(MetadataKey.PLATFORM, str)
        assert isinstance(MetadataKey.ENGAGEMENT, str)
        assert isinstance(MetadataKey.SOURCE_ENGINE, str)
        assert isinstance(MetadataKey.IS_SCRAPING_REQUIRED, str)

    def test_no_typo_vs_old_key(self):
        # Old code used "is_scrapping_required" (double-p). Ensure corrected.
        assert MetadataKey.IS_SCRAPING_REQUIRED == "is_scraping_required"

    def test_chunk_accepts_metadata_keys(self):
        chunk = _chunk("1", published_date="2024-01-01")
        assert MetadataKey.PUBLISHED_DATE in chunk.metadata


# ─── _parse_date ──────────────────────────────────────────────────────────────


class TestParseDate:
    def test_iso_with_tz(self):
        dt = _parse_date("2024-03-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2024 and dt.month == 3 and dt.day == 15

    def test_date_only(self):
        dt = _parse_date("2024-06-01")
        assert dt is not None
        assert dt.year == 2024 and dt.month == 6

    def test_naive_datetime_gets_utc(self):
        dt = _parse_date("2024-01-01T00:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_datetime_object_passthrough(self):
        now = datetime.now(timezone.utc)
        dt = _parse_date(now)
        assert dt == now

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_date("") is None

    def test_garbage_returns_none(self):
        assert _parse_date("not-a-date") is None


# ─── FreshnessBoostProcessor ──────────────────────────────────────────────────


class TestFreshnessBoost:
    def test_fresh_content_unchanged(self):
        proc = FreshnessBoostProcessor(half_life_days=30)
        chunk = _chunk("1", score=0.8, published_date=_days_ago(0))
        out = proc.process([chunk], [], _ctx())
        # 0 days old → multiplier ≈ 1.0
        assert abs(out[0].score - 0.8) < 0.01

    def test_half_life_halves_score(self):
        proc = FreshnessBoostProcessor(half_life_days=30, floor_multiplier=0.0)
        chunk = _chunk("1", score=1.0, published_date=_days_ago(30))
        out = proc.process([chunk], [], _ctx())
        # 30 days old with half_life=30 → multiplier = 0.5
        assert abs(out[0].score - 0.5) < 0.02

    def test_very_old_content_clamped_to_floor(self):
        proc = FreshnessBoostProcessor(half_life_days=7, floor_multiplier=0.1)
        chunk = _chunk("1", score=1.0, published_date=_days_ago(365))
        out = proc.process([chunk], [], _ctx())
        # Would decay to near 0 but floor = 0.1
        assert out[0].score >= 0.1

    def test_no_date_score_unchanged(self):
        proc = FreshnessBoostProcessor(half_life_days=30)
        chunk = _chunk("1", score=0.9)  # no published_date
        out = proc.process([chunk], [], _ctx())
        assert out[0].score == pytest.approx(0.9)

    def test_sorted_by_score_descending(self):
        proc = FreshnessBoostProcessor(half_life_days=7, floor_multiplier=0.0)
        chunks = [
            _chunk("old",   score=0.9, published_date=_days_ago(90)),
            _chunk("fresh", score=0.7, published_date=_days_ago(1)),
        ]
        out = proc.process(chunks, [], _ctx())
        # Fresh doc should beat old high-scorer after decay
        assert out[0].id == "fresh"

    def test_empty_input(self):
        proc = FreshnessBoostProcessor()
        assert proc.process([], [], _ctx()) == []

    def test_invalid_half_life_raises(self):
        with pytest.raises(ValueError):
            FreshnessBoostProcessor(half_life_days=0)

    def test_order_is_15(self):
        assert FreshnessBoostProcessor().order() == 15


# ─── QualityFilter max_age_days ───────────────────────────────────────────────


class TestQualityFilterMaxAge:
    def test_no_max_age_keeps_old_content(self):
        proc = QualityFilterProcessor(min_score=0.0, max_age_days=None)
        chunk = _chunk("old", score=0.9, published_date=_days_ago(3650))
        out = proc.process([chunk], [], _ctx())
        assert len(out) == 1

    def test_old_content_dropped(self):
        proc = QualityFilterProcessor(min_score=0.0, max_age_days=30)
        chunks = [
            _chunk("fresh", score=0.8, published_date=_days_ago(5)),
            _chunk("stale", score=0.9, published_date=_days_ago(60)),
        ]
        out = proc.process(chunks, [], _ctx())
        assert len(out) == 1
        assert out[0].id == "fresh"

    def test_no_date_chunk_kept(self):
        proc = QualityFilterProcessor(min_score=0.0, max_age_days=30)
        chunk = _chunk("noddate", score=0.8)  # no published_date
        out = proc.process([chunk], [], _ctx())
        # Safe default: keep chunks without date
        assert len(out) == 1

    def test_age_filter_applied_before_keep_best(self):
        proc = QualityFilterProcessor(
            min_score=0.99,       # very high threshold — would normally trigger keep_best
            max_age_days=30,
            keep_best_when_all_filtered=True,
        )
        stale = _chunk("stale", score=0.9, published_date=_days_ago(60))
        out = proc.process([stale], [], _ctx())
        # Stale content dropped even when keep_best=True and it's the "best"
        assert len(out) == 0

    def test_combined_score_and_age_filter(self):
        proc = QualityFilterProcessor(min_score=0.5, max_age_days=30)
        chunks = [
            _chunk("good",        score=0.8, published_date=_days_ago(5)),
            _chunk("low_score",   score=0.3, published_date=_days_ago(5)),
            _chunk("stale",       score=0.9, published_date=_days_ago(60)),
        ]
        out = proc.process(chunks, [], _ctx())
        assert len(out) == 1
        assert out[0].id == "good"
