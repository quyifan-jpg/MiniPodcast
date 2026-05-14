"""
In-memory BM25 keyword index over crawled_articles.

Why this exists:
  MySQL FULLTEXT NATURAL LANGUAGE MODE uses TF*IDF^2 scoring, which is
  not BM25. For honest BM25 ranking we build the index ourselves with
  rank-bm25 (Okapi BM25). The index is loaded once from MySQL, kept in
  memory, and rebuilt on a TTL.

Trade-offs:
  + True BM25 (k1=1.5, b=0.75 — Okapi defaults).
  + No extra infrastructure (no Elasticsearch / Meilisearch).
  + Sub-100ms queries on tens of thousands of docs.
  - Memory cost: tokens are kept in RAM. ~50MB per 10k articles of
    typical news length. Acceptable for current scale.
  - Index goes stale between refreshes. Default TTL is 10 minutes;
    callers that need fresh data can call `force_rebuild()`.

Usage:
    from rag.keyword_index import get_bm25_index
    index = get_bm25_index()
    hits = index.search("GPT-4o multimodal", top_k=20)
    # hits: list[(article_id: int, score: float)]
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from rank_bm25 import BM25Okapi


# ── Tokenizer ─────────────────────────────────────────────────────────
# Word-level tokenizer for English. Handles alphanumeric tokens with
# embedded hyphens / dots collapsed (so "GPT-4o" → "gpt", "4o";
# "v1.2.3" → "v1", "2", "3"). Good enough for tech news; for CJK
# content swap to a jieba-based tokenizer.
_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_PATTERN.findall(text)]


# ── Index data ────────────────────────────────────────────────────────


@dataclass
class _IndexSnapshot:
    """Immutable BM25 snapshot. Replaced atomically on rebuild."""

    bm25: BM25Okapi
    article_ids: list[int]  # parallel to bm25's corpus
    built_at: float  # unix seconds


# ── Singleton index with TTL refresh ──────────────────────────────────


class BM25Index:
    """
    Thread-safe BM25 index over crawled_articles. Lazy-loaded on first
    use, refreshed on TTL.
    """

    def __init__(self, *, ttl_seconds: float = 600.0):
        self._ttl_s = ttl_seconds
        self._snapshot: Optional[_IndexSnapshot] = None
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────

    def search(self, query: str, *, top_k: int = 20) -> list[tuple[int, float]]:
        """
        Return top-k (article_id, score) for the query.

        Score is the raw BM25 score (not normalized). Higher = better.
        Returns empty list if the corpus is empty or query has no
        valid tokens.
        """
        snapshot = self._get_or_build()
        if snapshot is None or not snapshot.article_ids:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = snapshot.bm25.get_scores(tokens)
        # Rank by score, take top-k that have non-zero relevance.
        # numpy.argsort is fastest path; we slice top-k from the tail.
        import numpy as np

        order = np.argsort(scores)[::-1][:top_k]

        results: list[tuple[int, float]] = []
        for idx in order:
            score = float(scores[idx])
            if score <= 0.0:
                break
            results.append((snapshot.article_ids[int(idx)], score))
        return results

    def force_rebuild(self) -> None:
        """Drop the cached snapshot; next search() will rebuild."""
        with self._lock:
            self._snapshot = None

    def stats(self) -> dict:
        """Diagnostic info for /health endpoints."""
        snap = self._snapshot
        if snap is None:
            return {"built": False, "docs": 0, "age_s": None}
        return {
            "built": True,
            "docs": len(snap.article_ids),
            "age_s": round(time.time() - snap.built_at, 1),
            "ttl_s": self._ttl_s,
        }

    # ── Internals ──────────────────────────────────────────────────

    def _get_or_build(self) -> Optional[_IndexSnapshot]:
        snap = self._snapshot
        now = time.time()

        # Hot path: snapshot fresh, no lock needed.
        if snap is not None and (now - snap.built_at) < self._ttl_s:
            return snap

        # Cold / stale: rebuild under lock.
        with self._lock:
            snap = self._snapshot
            if snap is not None and (now - snap.built_at) < self._ttl_s:
                return snap
            self._snapshot = self._build()
            return self._snapshot

    def _build(self) -> Optional[_IndexSnapshot]:
        start = time.perf_counter()
        try:
            rows = self._fetch_corpus()
        except Exception as e:
            logger.error("BM25Index: corpus fetch failed: {e}", e=e)
            return _IndexSnapshot(
                bm25=BM25Okapi([[""]]),
                article_ids=[],
                built_at=time.time(),
            )

        if not rows:
            logger.warning("BM25Index: corpus is empty")
            return _IndexSnapshot(
                bm25=BM25Okapi([[""]]),
                article_ids=[],
                built_at=time.time(),
            )

        article_ids: list[int] = []
        tokens_corpus: list[list[str]] = []
        for row in rows:
            text = " ".join(str(row.get(col, "") or "") for col in ("title", "summary", "content"))
            tokens = _tokenize(text)
            if not tokens:
                # rank-bm25 errors on empty token lists; insert a
                # placeholder so positional indexing stays stable.
                tokens = [""]
            article_ids.append(int(row["id"]))
            tokens_corpus.append(tokens)

        bm25 = BM25Okapi(tokens_corpus)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "BM25Index: built over {n} docs in {ms:.0f}ms",
            n=len(article_ids),
            ms=elapsed_ms,
        )
        return _IndexSnapshot(
            bm25=bm25,
            article_ids=article_ids,
            built_at=time.time(),
        )

    @staticmethod
    def _fetch_corpus() -> list[dict]:
        """Pull processed articles from MySQL into memory."""
        from db.config import get_tracking_db_path
        from db.connection import execute_query

        db_path = get_tracking_db_path()
        return execute_query(
            db_path,
            """
            SELECT id, title, summary, content
            FROM crawled_articles
            WHERE processed = 1
            """,
            (),
            fetch=True,
        )


# ── Module-level singleton ────────────────────────────────────────────


_index: Optional[BM25Index] = None
_index_lock = threading.Lock()


def get_bm25_index(*, ttl_seconds: float = 600.0) -> BM25Index:
    """Return the process-wide BM25 index singleton."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = BM25Index(ttl_seconds=ttl_seconds)
    return _index
