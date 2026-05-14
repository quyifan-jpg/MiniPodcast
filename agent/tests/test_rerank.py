"""
Tests for RerankProcessor — no real API calls, Jina/Cohere are mocked.

Run:
    cd agent && python3 run_tests.py tests/test_rerank.py -v

What is tested:
  1. is_enabled() gate  — disabled when no key, active when key present
  2. Title is passed    — documents sent to Jina include {"text", "title"}
  3. Scores are updated — chunk.score reflects the API's relevance_score
  4. Order is preserved — results sorted by descending relevance_score
  5. Fallback on error  — API failure returns original list unchanged
  6. <2 chunks skipped  — single chunk passes through without API call
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rag.models import ChannelResult, ChannelType, RetrievedChunk, SearchContext
from rag.postprocessors.rerank import RerankProcessor


# ── helpers ───────────────────────────────────────────────────────────────────

def _ctx(query: str = "AI in healthcare", top_k: int = 5) -> SearchContext:
    return SearchContext(original_query=query, top_k=top_k)


def _chunk(id: str, title: str, score: float = 0.5) -> RetrievedChunk:
    return RetrievedChunk(
        id=id,
        content=f"Content about {title}. " * 10,
        title=title,
        url=f"https://example.com/{id}",
        score=score,
        source_channel=ChannelType.CHUNK_VECTOR,
    )


def _jina_response(results: list[dict]) -> MagicMock:
    """Build a fake requests.Response with Jina's JSON shape."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"results": results}
    return mock_resp


# ── is_enabled ────────────────────────────────────────────────────────────────

class TestIsEnabled:
    def test_disabled_when_no_key(self):
        proc = RerankProcessor(provider="jina", api_key=None)
        assert proc.is_enabled(_ctx()) is False

    def test_disabled_when_provider_disabled(self):
        proc = RerankProcessor(provider="disabled", api_key="jina_xxx")
        assert proc.is_enabled(_ctx()) is False

    def test_enabled_with_key(self):
        proc = RerankProcessor(provider="jina", api_key="jina_xxx")
        assert proc.is_enabled(_ctx()) is True

    def test_order_is_10(self):
        assert RerankProcessor().order() == 10


# ── Jina: title passing ───────────────────────────────────────────────────────

class TestJinaRerank:
    def _make_proc(self, model="jina-reranker-v2-base-multilingual"):
        return RerankProcessor(provider="jina", api_key="jina_test_key", model=model)

    def test_title_included_in_request(self):
        """Documents sent to Jina must include title, not just plain strings."""
        proc = self._make_proc()
        chunks = [
            _chunk("a", "Deep Learning Basics", score=0.6),
            _chunk("b", "Quantum Physics Overview", score=0.8),
        ]

        captured_payload = {}

        def fake_post(url, *, headers, json, timeout):
            captured_payload.update(json)
            return _jina_response([
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.3},
            ])

        with patch("requests.post", side_effect=fake_post):
            proc.process(chunks, [], _ctx())

        docs = captured_payload["documents"]
        assert isinstance(docs[0], dict), "document should be a dict, not a plain string"
        assert "text" in docs[0]
        assert "title" in docs[0]
        assert docs[0]["title"] == "Deep Learning Basics"
        assert docs[1]["title"] == "Quantum Physics Overview"

    def test_scores_updated_from_api(self):
        """chunk.score must reflect relevance_score returned by Jina."""
        proc = self._make_proc()
        chunks = [
            _chunk("a", "Article A", score=0.9),   # high RRF score
            _chunk("b", "Article B", score=0.5),
        ]

        with patch("requests.post", return_value=_jina_response([
            {"index": 0, "relevance_score": 0.2},   # Jina says A is NOT relevant
            {"index": 1, "relevance_score": 0.95},  # Jina says B IS relevant
        ])):
            result = proc.process(chunks, [], _ctx())

        assert result[0].id == "b"   # B now ranks first
        assert result[1].id == "a"
        assert abs(result[0].score - 0.95) < 0.001
        assert abs(result[1].score - 0.2)  < 0.001

    def test_sorted_by_relevance_score(self):
        proc = self._make_proc()
        chunks = [_chunk(str(i), f"Article {i}") for i in range(4)]

        # Jina returns scores in reverse order
        with patch("requests.post", return_value=_jina_response([
            {"index": 0, "relevance_score": 0.1},
            {"index": 1, "relevance_score": 0.9},
            {"index": 2, "relevance_score": 0.5},
            {"index": 3, "relevance_score": 0.7},
        ])):
            result = proc.process(chunks, [], _ctx(top_k=4))

        scores = [r.score for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_query_passed_correctly(self):
        proc = self._make_proc()
        chunks = [_chunk("a", "A"), _chunk("b", "B")]
        captured = {}

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _jina_response([
                {"index": 0, "relevance_score": 0.8},
                {"index": 1, "relevance_score": 0.4},
            ])

        with patch("requests.post", side_effect=fake_post):
            proc.process(chunks, [], _ctx(query="AI in healthcare"))

        assert captured["query"] == "AI in healthcare"

    def test_bearer_token_in_header(self):
        proc = self._make_proc()
        chunks = [_chunk("a", "A"), _chunk("b", "B")]
        captured_headers = {}

        def fake_post(url, *, headers, json, timeout):
            captured_headers.update(headers)
            return _jina_response([
                {"index": 0, "relevance_score": 0.8},
                {"index": 1, "relevance_score": 0.4},
            ])

        with patch("requests.post", side_effect=fake_post):
            proc.process(chunks, [], _ctx())

        assert captured_headers["Authorization"] == "Bearer jina_test_key"

    def test_api_error_returns_original_order(self):
        """If Jina API fails, original chunk list is returned unchanged."""
        proc = self._make_proc()
        chunks = [_chunk("a", "A", score=0.9), _chunk("b", "B", score=0.5)]
        original_scores = [c.score for c in chunks]

        def fail_post(*args, **kwargs):
            raise ConnectionError("network error")

        with patch("requests.post", side_effect=fail_post):
            result = proc.process(chunks, [], _ctx())

        assert [r.id for r in result] == ["a", "b"]   # order unchanged
        assert [r.score for r in result] == original_scores

    def test_single_chunk_skips_api(self):
        """<2 chunks → API is never called."""
        proc = self._make_proc()
        chunk = _chunk("a", "A")

        with patch("requests.post") as mock_post:
            result = proc.process([chunk], [], _ctx())

        mock_post.assert_not_called()
        assert result == [chunk]

    def test_chunk_without_title_sends_plain_string(self):
        """Chunks with empty title fall back to plain content string."""
        proc = self._make_proc()
        chunks = [
            RetrievedChunk(id="1", content="some content", title="", url="", score=0.5,
                           source_channel=ChannelType.CHUNK_VECTOR),
            RetrievedChunk(id="2", content="more content", title="", url="", score=0.4,
                           source_channel=ChannelType.CHUNK_VECTOR),
        ]
        captured = {}

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _jina_response([
                {"index": 0, "relevance_score": 0.7},
                {"index": 1, "relevance_score": 0.3},
            ])

        with patch("requests.post", side_effect=fake_post):
            proc.process(chunks, [], _ctx())

        # No title → plain string, not a dict
        assert isinstance(captured["documents"][0], str)


# ── Pipeline integration: is rerank actually in the chain? ───────────────────

class TestPipelineIntegration:
    """Verifies RerankProcessor is wired into factory when config says so."""

    def test_factory_includes_rerank_when_enabled(self):
        """
        Patch rag_settings so rerank is enabled, then check that
        create_retrieval_engine() includes a RerankProcessor.
        """
        from unittest.mock import patch as _patch

        fake_settings = MagicMock()
        fake_settings.chunk_vector_enabled   = False
        fake_settings.article_vector_enabled = False
        fake_settings.keyword_enabled        = False
        fake_settings.social_media_enabled   = False
        fake_settings.external_search_enabled = False
        fake_settings.dedup_enabled          = False
        fake_settings.rrf_enabled            = False
        fake_settings.freshness_enabled      = False
        fake_settings.quality_filter_enabled = False
        fake_settings.channel_timeout_s      = 30.0

        # Enable rerank
        fake_settings.rerank_enabled   = True
        fake_settings.rerank_provider  = "jina"
        fake_settings.rerank_api_key   = "jina_test"
        fake_settings.rerank_model     = "jina-reranker-v2-base-multilingual"

        with _patch("rag.factory.rag_settings", fake_settings):
            from rag.factory import create_retrieval_engine
            engine = create_retrieval_engine()

        processor_types = [type(p).__name__ for p in engine._postprocessors]
        assert "RerankProcessor" in processor_types

    def test_factory_excludes_rerank_when_disabled(self):
        from unittest.mock import patch as _patch

        fake_settings = MagicMock()
        fake_settings.chunk_vector_enabled   = False
        fake_settings.article_vector_enabled = False
        fake_settings.keyword_enabled        = False
        fake_settings.social_media_enabled   = False
        fake_settings.external_search_enabled = False
        fake_settings.dedup_enabled          = False
        fake_settings.rrf_enabled            = False
        fake_settings.freshness_enabled      = False
        fake_settings.quality_filter_enabled = False
        fake_settings.channel_timeout_s      = 30.0
        fake_settings.rerank_enabled         = False   # disabled

        with _patch("rag.factory.rag_settings", fake_settings):
            from rag.factory import create_retrieval_engine
            engine = create_retrieval_engine()

        processor_types = [type(p).__name__ for p in engine._postprocessors]
        assert "RerankProcessor" not in processor_types
