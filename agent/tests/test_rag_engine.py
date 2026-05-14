"""
Unit tests for the multi-channel retrieval engine.

Tests the engine orchestration, post-processors, and channel interface
without requiring external dependencies (FAISS, OpenAI, MySQL).

Run:
    cd agent && python -m pytest tests/test_rag_engine.py -v
"""

from __future__ import annotations

import asyncio

from rag.models import (
    ChannelResult,
    ChannelType,
    RetrievedChunk,
    SearchContext,
)
from rag.channels.base import SearchChannel
from rag.postprocessors.dedup import DeduplicationProcessor
from rag.postprocessors.rrf import RRFProcessor
from rag.postprocessors.quality_filter import QualityFilterProcessor
from rag.engine import MultiChannelRetrievalEngine


# ═══════════════════════════════════════════════════════════════════════
# Test fixtures — mock channels
# ═══════════════════════════════════════════════════════════════════════


class MockChannel(SearchChannel):
    """A controllable mock channel for testing."""

    def __init__(
        self,
        channel_type: ChannelType,
        prio: int,
        results: list[RetrievedChunk],
        enabled: bool = True,
        raise_error: bool = False,
    ):
        self._type = channel_type
        self._priority = prio
        self._results = results
        self._enabled = enabled
        self._raise_error = raise_error

    def channel_type(self) -> ChannelType:
        return self._type

    def priority(self) -> int:
        return self._priority

    def is_enabled(self, context: SearchContext) -> bool:
        return self._enabled

    async def search(self, context: SearchContext) -> ChannelResult:
        if self._raise_error:
            raise RuntimeError("Mock channel failure")
        return ChannelResult(
            channel_type=self._type,
            chunks=self._results,
            latency_ms=10.0,
        )


def _chunk(
    id: str,
    content: str = "test content",
    url: str = "",
    score: float = 0.8,
    channel: ChannelType = ChannelType.CHUNK_VECTOR,
) -> RetrievedChunk:
    return RetrievedChunk(
        id=id,
        content=content,
        title=f"Title {id}",
        url=url or f"https://example.com/{id}",
        score=score,
        source_channel=channel,
    )


def _ctx(query: str = "test query", top_k: int = 10) -> SearchContext:
    return SearchContext(original_query=query, top_k=top_k)


# ═══════════════════════════════════════════════════════════════════════
# Model tests
# ═══════════════════════════════════════════════════════════════════════


class TestModels:
    def test_dedup_key_uses_url(self):
        chunk = _chunk("1", url="https://example.com/article")
        assert chunk.dedup_key == "https://example.com/article"

    def test_dedup_key_falls_back_to_id(self):
        chunk = RetrievedChunk(id="42", content="test", url="")
        assert chunk.dedup_key == "42"

    def test_channel_result_empty(self):
        result = ChannelResult.empty(ChannelType.KEYWORD)
        assert result.channel_type == ChannelType.KEYWORD
        assert result.chunks == []
        assert result.latency_ms == 0.0

    def test_effective_query_prefers_rewritten(self):
        ctx = SearchContext(original_query="K8s", rewritten_query="Kubernetes")
        assert ctx.effective_query == "Kubernetes"

    def test_effective_query_falls_back_to_original(self):
        ctx = SearchContext(original_query="K8s")
        assert ctx.effective_query == "K8s"


# ═══════════════════════════════════════════════════════════════════════
# Deduplication tests
# ═══════════════════════════════════════════════════════════════════════


class TestDeduplication:
    def test_no_duplicates_pass_through(self):
        processor = DeduplicationProcessor()
        chunks = [_chunk("1"), _chunk("2"), _chunk("3")]
        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, chunks),
        ]
        output = processor.process(chunks, results, _ctx())
        assert len(output) == 3

    def test_duplicates_removed_by_url(self):
        processor = DeduplicationProcessor()
        c1 = _chunk("1", url="https://same.com", score=0.9, channel=ChannelType.CHUNK_VECTOR)
        c2 = _chunk("2", url="https://same.com", score=0.7, channel=ChannelType.KEYWORD)
        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, [c1]),
            ChannelResult(ChannelType.KEYWORD, [c2]),
        ]
        output = processor.process([c1, c2], results, _ctx())
        assert len(output) == 1
        # Higher priority channel (CHUNK_VECTOR=1) wins
        assert output[0].source_channel == ChannelType.CHUNK_VECTOR
        # But takes max score
        assert output[0].score == 0.9

    def test_higher_score_preserved_from_lower_priority(self):
        processor = DeduplicationProcessor()
        c1 = _chunk("1", url="https://same.com", score=0.5, channel=ChannelType.CHUNK_VECTOR)
        c2 = _chunk("2", url="https://same.com", score=0.95, channel=ChannelType.KEYWORD)
        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, [c1]),
            ChannelResult(ChannelType.KEYWORD, [c2]),
        ]
        output = processor.process([c1, c2], results, _ctx())
        assert len(output) == 1
        assert output[0].source_channel == ChannelType.CHUNK_VECTOR  # priority wins
        assert output[0].score == 0.95  # max score wins

    def test_empty_input(self):
        processor = DeduplicationProcessor()
        output = processor.process([], [], _ctx())
        assert output == []


# ═══════════════════════════════════════════════════════════════════════
# RRF tests
# ═══════════════════════════════════════════════════════════════════════


class TestRRF:
    def test_single_channel_skipped(self):
        processor = RRFProcessor(k=60)
        chunks = [_chunk("1", score=0.9), _chunk("2", score=0.7)]
        results = [ChannelResult(ChannelType.CHUNK_VECTOR, chunks)]
        output = processor.process(chunks, results, _ctx())
        # Should pass through unchanged
        assert len(output) == 2
        assert output[0].score == 0.9  # unchanged

    def test_two_channels_fusion(self):
        processor = RRFProcessor(k=60)

        c1_a = _chunk("A", url="https://a.com", score=0.95, channel=ChannelType.CHUNK_VECTOR)
        c1_b = _chunk("B", url="https://b.com", score=0.80, channel=ChannelType.CHUNK_VECTOR)
        c2_b = _chunk("B2", url="https://b.com", score=0.90, channel=ChannelType.KEYWORD)
        c2_c = _chunk("C", url="https://c.com", score=0.85, channel=ChannelType.KEYWORD)

        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, [c1_a, c1_b]),
            ChannelResult(ChannelType.KEYWORD, [c2_b, c2_c]),
        ]
        all_chunks = [c1_a, c1_b, c2_b, c2_c]

        output = processor.process(all_chunks, results, _ctx())

        # B appears in both channels → should get highest RRF score
        urls = [c.url for c in output]
        assert "https://b.com" in urls

        # All scores should be in [0, 1]
        for c in output:
            assert 0 <= c.score <= 1.0

        # B should be ranked first (appears in both channels)
        assert output[0].url == "https://b.com"

    def test_rrf_scores_normalized(self):
        processor = RRFProcessor(k=60)
        c1 = _chunk("1", url="https://a.com", channel=ChannelType.CHUNK_VECTOR)
        c2 = _chunk("2", url="https://b.com", channel=ChannelType.KEYWORD)
        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, [c1]),
            ChannelResult(ChannelType.KEYWORD, [c2]),
        ]
        output = processor.process([c1, c2], results, _ctx())
        # Max score should be 1.0 after normalization
        max_score = max(c.score for c in output)
        assert max_score == 1.0

    def test_empty_channels_filtered(self):
        processor = RRFProcessor(k=60)
        c1 = _chunk("1")
        results = [
            ChannelResult(ChannelType.CHUNK_VECTOR, [c1]),
            ChannelResult(ChannelType.KEYWORD, []),  # empty
        ]
        output = processor.process([c1], results, _ctx())
        # Only 1 effective channel → skip RRF
        assert len(output) == 1


# ═══════════════════════════════════════════════════════════════════════
# Quality filter tests
# ═══════════════════════════════════════════════════════════════════════


class TestQualityFilter:
    def test_filters_low_score(self):
        processor = QualityFilterProcessor(min_score=0.5, min_content_length=1)
        chunks = [
            _chunk("1", score=0.8, content="good content"),
            _chunk("2", score=0.2, content="bad content"),
        ]
        output = processor.process(chunks, [], _ctx())
        assert len(output) == 1
        assert output[0].id == "1"

    def test_filters_short_content(self):
        processor = QualityFilterProcessor(min_score=0.0, min_content_length=20)
        chunks = [
            _chunk("1", content="a" * 50),
            _chunk("2", content="short"),
        ]
        output = processor.process(chunks, [], _ctx())
        assert len(output) == 1

    def test_keeps_best_when_all_filtered(self):
        processor = QualityFilterProcessor(
            min_score=0.99,
            keep_best_when_all_filtered=True,
        )
        chunks = [_chunk("1", score=0.5), _chunk("2", score=0.8)]
        output = processor.process(chunks, [], _ctx())
        assert len(output) == 1
        assert output[0].id == "2"  # highest score

    def test_returns_empty_when_keep_best_disabled(self):
        processor = QualityFilterProcessor(
            min_score=0.99,
            keep_best_when_all_filtered=False,
        )
        chunks = [_chunk("1", score=0.5)]
        output = processor.process(chunks, [], _ctx())
        assert output == []

    def test_truncates_to_top_k(self):
        processor = QualityFilterProcessor(min_score=0.0)
        chunks = [_chunk(str(i), score=0.9) for i in range(20)]
        output = processor.process(chunks, [], _ctx(top_k=5))
        assert len(output) == 5


# ═══════════════════════════════════════════════════════════════════════
# Engine integration tests
# ═══════════════════════════════════════════════════════════════════════


class TestEngine:
    def test_parallel_execution(self):
        chunks_a = [_chunk("1", channel=ChannelType.CHUNK_VECTOR)]
        chunks_b = [_chunk("2", channel=ChannelType.KEYWORD)]

        engine = MultiChannelRetrievalEngine(
            channels=[
                MockChannel(ChannelType.CHUNK_VECTOR, 1, chunks_a),
                MockChannel(ChannelType.KEYWORD, 3, chunks_b),
            ],
            postprocessors=[],
        )
        results = asyncio.run(engine.retrieve(_ctx()))
        assert len(results) == 2

    def test_channel_failure_isolated(self):
        """One channel fails → other channels still return results."""
        chunks_ok = [_chunk("1")]

        engine = MultiChannelRetrievalEngine(
            channels=[
                MockChannel(ChannelType.CHUNK_VECTOR, 1, chunks_ok),
                MockChannel(ChannelType.KEYWORD, 3, [], raise_error=True),
            ],
            postprocessors=[],
        )
        results = asyncio.run(engine.retrieve(_ctx()))
        assert len(results) == 1

    def test_disabled_channel_skipped(self):
        engine = MultiChannelRetrievalEngine(
            channels=[
                MockChannel(ChannelType.CHUNK_VECTOR, 1, [_chunk("1")], enabled=False),
                MockChannel(
                    ChannelType.KEYWORD,
                    3,
                    [
                        _chunk("2", channel=ChannelType.KEYWORD),
                    ],
                ),
            ],
            postprocessors=[],
        )
        results = asyncio.run(engine.retrieve(_ctx()))
        assert len(results) == 1
        assert results[0].source_channel == ChannelType.KEYWORD

    def test_full_pipeline(self):
        """End-to-end: channels → dedup → RRF → quality filter."""
        # Same URL in two channels
        c1 = _chunk("1", url="https://shared.com", score=0.9, channel=ChannelType.CHUNK_VECTOR)
        c2 = _chunk("2", url="https://shared.com", score=0.7, channel=ChannelType.KEYWORD)
        c3 = _chunk("3", url="https://unique.com", score=0.85, channel=ChannelType.CHUNK_VECTOR)
        c4 = _chunk("4", url="https://low.com", score=0.1, channel=ChannelType.KEYWORD, content="x")

        engine = MultiChannelRetrievalEngine(
            channels=[
                MockChannel(ChannelType.CHUNK_VECTOR, 1, [c1, c3]),
                MockChannel(ChannelType.KEYWORD, 3, [c2, c4]),
            ],
            postprocessors=[
                DeduplicationProcessor(),
                RRFProcessor(k=60),
                QualityFilterProcessor(min_score=0.2, min_content_length=5),
            ],
        )
        results = asyncio.run(engine.retrieve(_ctx(top_k=10)))

        # c4 should be filtered (score too low or content too short)
        urls = [r.url for r in results]
        assert "https://shared.com" in urls  # deduplicated to 1
        assert "https://unique.com" in urls

    def test_empty_results(self):
        engine = MultiChannelRetrievalEngine(
            channels=[
                MockChannel(ChannelType.CHUNK_VECTOR, 1, []),
            ],
            postprocessors=[DeduplicationProcessor()],
        )
        results = asyncio.run(engine.retrieve(_ctx()))
        assert results == []

    def test_postprocessor_chain_order(self):
        """Post-processors run in order() ascending."""
        call_order = []

        class TrackingProcessor(QualityFilterProcessor):
            def __init__(self, name: str, order_val: int):
                super().__init__(min_score=0.0)
                self._name = name
                self._order_val = order_val

            def order(self):
                return self._order_val

            def process(self, chunks, all_results, context):
                call_order.append(self._name)
                return chunks

        engine = MultiChannelRetrievalEngine(
            channels=[MockChannel(ChannelType.CHUNK_VECTOR, 1, [_chunk("1")])],
            postprocessors=[
                TrackingProcessor("third", 20),
                TrackingProcessor("first", 1),
                TrackingProcessor("second", 5),
            ],
        )
        asyncio.run(engine.retrieve(_ctx()))
        assert call_order == ["first", "second", "third"]
