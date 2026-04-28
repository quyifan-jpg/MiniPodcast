"""
Vector index sync service — single authority for keeping Milvus aligned
with MySQL (the source of truth).

State machine on `crawled_articles.embedding_status`:
    pending  → newly analyzed, awaits embedding
    indexing → currently being embedded (claimed by a worker)
    indexed  → vector in Milvus, in sync with content_hash
    stale    → content_hash changed since last index, needs re-embed
    failed   → embed attempt failed; will be retried by the next run
    disabled → explicitly excluded from RAG (admin action)

Drift detection:
    indexed_hash != content_hash  →  stale

Public API mirrors CAgent's KnowledgeChunkServiceImpl:
    rebuild_article(id)       ≈ rebuildByDocId
    enable_article(id)        ≈ enableChunk (article level)
    disable_article(id)       ≈ disableChunk (article level)
    remove_article(id)        ≈ delete vectors on soft-delete
    index_pending(batch)      ≈ batch repair
    mark_for_indexing(...)    ≈ post-AI hook
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from loguru import logger
from openai import OpenAI

from db.config import get_tracking_db_path
from db.connection import db_connection, execute_query
from db.milvus import get_milvus
from utils.load_api_keys import load_api_key

EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 300
CHUNK_OVERLAP = 50

# ── Status constants ──────────────────────────────────────────────────
STATUS_PENDING = "pending"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

# A worker may claim an article in any of these states.
CLAIMABLE_STATUSES = (STATUS_PENDING, STATUS_STALE, STATUS_FAILED)


# ── Helpers ───────────────────────────────────────────────────────────

def _compute_content_hash(title: str, content: str) -> str:
    """SHA-256 of title + content. Stable identity for index freshness."""
    h = hashlib.sha256()
    h.update((title or "").encode("utf-8"))
    h.update(b"\n")
    h.update((content or "").encode("utf-8"))
    return h.hexdigest()


def _split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if not text or not text.strip():
        return []
    words = text.split()
    if len(words) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(words):
        end = min(start + size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = end - overlap
    return chunks


def _build_chunk_texts(article: dict) -> list[str]:
    title = (article.get("title") or "").strip()
    body = (article.get("content") or article.get("summary") or "").strip()
    if not body:
        return [title] if title else []
    chunks = _split_chunks(body)
    if chunks and title:
        chunks[0] = f"{title}\n\n{chunks[0]}"
    return chunks


# ── Service ───────────────────────────────────────────────────────────

class VectorIndexService:
    """Façade over Milvus that enforces the MySQL-driven state machine."""

    def __init__(self) -> None:
        self._openai: Optional[OpenAI] = None

    # ── OpenAI client (cached) ────────────────────────────────────────

    def _client(self) -> OpenAI:
        if self._openai is None:
            key = load_api_key("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY not configured")
            self._openai = OpenAI(api_key=key)
        return self._openai

    def _embed(self, text: str) -> list[float]:
        return self._embed_batch([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Embed many texts in a single API call.

        OpenAI's embeddings endpoint accepts a list of strings and returns
        vectors in the same order. Batching turns N+1 sequential round-
        trips per article (1 article-level + N chunks) into a single
        request, which dominates rebuild latency.
        """
        if not texts:
            return []
        resp = self._client().embeddings.create(input=texts, model=EMBEDDING_MODEL)
        # Defensive: API guarantees same-order returns, but assert it once
        # in case of future SDK changes.
        return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]

    # ── Hook: post-AI ─────────────────────────────────────────────────

    def mark_for_indexing(self, article_id: int, title: str, content: str) -> str:
        """
        Called when AI analysis succeeds. Computes content_hash and routes
        the article to the correct state:

            * disabled         → just refresh content_hash; do not re-enable
            * indexed + same   → no-op (already up to date)
            * indexed + drift  → mark stale
            * anything else    → mark pending

        Returns the new content_hash (so callers can persist it elsewhere
        if useful).
        """
        new_hash = _compute_content_hash(title, content)
        db_path = get_tracking_db_path()

        with db_connection(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT content_hash, indexed_hash, embedding_status "
                "FROM crawled_articles WHERE id = %s",
                (article_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.warning("mark_for_indexing: article {id} not found", id=article_id)
                return new_hash

            current_status = row.get("embedding_status")
            indexed_hash = row.get("indexed_hash")

            if current_status == STATUS_DISABLED:
                # Honor the admin's disable; only refresh the hash so the
                # next enable_article will produce a correct rebuild.
                cur.execute(
                    "UPDATE crawled_articles SET content_hash=%s WHERE id=%s",
                    (new_hash, article_id),
                )
            elif current_status == STATUS_INDEXED and indexed_hash == new_hash:
                # Identical content already indexed — nothing to do.
                pass
            elif current_status == STATUS_INDEXED:
                cur.execute(
                    "UPDATE crawled_articles "
                    "SET content_hash=%s, embedding_status=%s WHERE id=%s",
                    (new_hash, STATUS_STALE, article_id),
                )
            else:
                cur.execute(
                    "UPDATE crawled_articles "
                    "SET content_hash=%s, embedding_status=%s WHERE id=%s",
                    (new_hash, STATUS_PENDING, article_id),
                )
            conn.commit()
        return new_hash

    # ── Core: per-article rebuild ─────────────────────────────────────

    def rebuild_article(self, article_id: int) -> bool:
        """
        Full rebuild for one article (CAgent's `rebuildByDocId` equivalent):
            1. Atomically claim the row (status → indexing)
            2. Read latest article text from MySQL
            3. Wipe old vectors in Milvus + chunk rows in MySQL
            4. Re-chunk, re-embed, re-insert
            5. Mark indexed + record indexed_hash + last_indexed_at

        Returns True on a successful rebuild, False if the article was
        already up to date, claimed by another worker, or failed.
        """
        db_path = get_tracking_db_path()

        # 1. Claim — only proceed if status is one we can take.
        if not self._claim(article_id):
            logger.debug(
                "Article {id} not claimable (already indexing/indexed/disabled)",
                id=article_id,
            )
            return False

        try:
            article = execute_query(
                db_path,
                "SELECT id, title, url, summary, content, content_hash "
                "FROM crawled_articles WHERE id=%s",
                (article_id,),
                fetch_one=True,
            )
            if not article:
                logger.warning("rebuild: article {id} disappeared", id=article_id)
                return False

            # Derive content_hash if it was never set (first-time indexing
            # of legacy rows that pre-date the AI hook).
            content_hash = article.get("content_hash") or _compute_content_hash(
                article.get("title") or "",
                article.get("content") or "",
            )

            title = (article.get("title") or "")[:1024]
            url = (article.get("url") or "")[:2048]
            summary = (article.get("summary") or article.get("content") or "")[:4096]

            # 2. Embed everything before mutating Milvus, so a failure
            #    leaves the existing index intact. One batched API call
            #    covers the article-level vector and every chunk.
            article_text = (
                f"Title: {article.get('title', '')}\n\n"
                f"Summary: {article.get('summary', '')}\n\n"
                f"Content: {article.get('content', '')}"
            )
            chunk_texts = _build_chunk_texts(article)
            all_vectors = self._embed_batch([article_text, *chunk_texts])
            article_vector = all_vectors[0]
            chunk_vectors = all_vectors[1:]

            mv = get_milvus()

            # 3. Wipe old vectors + old chunk rows.
            mv.delete_article(article_id)
            with db_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM article_chunks WHERE article_id=%s",
                    (article_id,),
                )
                conn.commit()

            # 4. Insert article-level vector.
            mv.insert_articles([{
                "id": article_id,
                "title": title,
                "url": url,
                "summary": summary,
                "vector": article_vector,
            }])

            # 5. Insert chunks (MySQL row first to mint id, then Milvus).
            if chunk_texts:
                chunk_ids: list[int] = []
                with db_connection(db_path) as conn:
                    cur = conn.cursor()
                    now_iso = datetime.now().isoformat()
                    for idx, ct in enumerate(chunk_texts):
                        cur.execute(
                            "INSERT INTO article_chunks "
                            "(article_id, chunk_index, chunk_text, created_at) "
                            "VALUES (%s, %s, %s, %s)",
                            (article_id, idx, ct, now_iso),
                        )
                        chunk_ids.append(cur.lastrowid)
                    conn.commit()

                rows = [{
                    "id": cid,
                    "article_id": article_id,
                    "chunk_index": idx,
                    "chunk_text": ct[:8192],
                    "title": title,
                    "url": url,
                    "vector": vec,
                } for idx, (cid, ct, vec) in enumerate(
                    zip(chunk_ids, chunk_texts, chunk_vectors)
                )]
                mv.insert_chunks(rows)

            # 6. Mark indexed.
            self._mark_indexed(article_id, content_hash)

            logger.info(
                "Rebuilt article {id}: 1 article vec + {n} chunk vec(s)",
                id=article_id,
                n=len(chunk_texts),
            )
            return True

        except Exception as e:
            logger.error(
                "Rebuild failed for article {id}: {e}",
                id=article_id,
                e=str(e),
            )
            self._mark_failed(article_id)
            return False

    # ── Admin-level operations ────────────────────────────────────────

    def disable_article(self, article_id: int) -> None:
        """Remove an article from RAG: drop vectors + flip enabled flag."""
        try:
            get_milvus().delete_article(article_id)
        except Exception as e:
            logger.warning(
                "Milvus delete failed during disable for {id}: {e}",
                id=article_id,
                e=str(e),
            )

        with db_connection(get_tracking_db_path()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE crawled_articles "
                "SET embedding_enabled=0, embedding_status=%s WHERE id=%s",
                (STATUS_DISABLED, article_id),
            )
            conn.commit()
        logger.info("Disabled article {id}", id=article_id)

    def enable_article(self, article_id: int) -> bool:
        """Re-enable an article and trigger a fresh rebuild."""
        with db_connection(get_tracking_db_path()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE crawled_articles "
                "SET embedding_enabled=1, embedding_status=%s WHERE id=%s",
                (STATUS_PENDING, article_id),
            )
            conn.commit()
        return self.rebuild_article(article_id)

    def remove_article(self, article_id: int) -> None:
        """Hook for soft delete: drop vectors. MySQL row stays (deleted_at)."""
        try:
            get_milvus().delete_article(article_id)
        except Exception as e:
            logger.warning(
                "Milvus delete failed for removed article {id}: {e}",
                id=article_id,
                e=str(e),
            )

    # ── Batch driver ──────────────────────────────────────────────────

    def index_pending(self, batch_size: int = 50) -> dict:
        """
        Process up to `batch_size` claimable articles. Used by the legacy
        embedding/chunk processors and the periodic Celery beat task.

        Priority: failed > stale > pending (so retries clear faster than
        net-new work).
        """
        placeholders = ",".join(["%s"] * len(CLAIMABLE_STATUSES))
        rows = execute_query(
            get_tracking_db_path(),
            f"SELECT id FROM crawled_articles "
            f"WHERE embedding_enabled = 1 "
            f"  AND ai_status = 'success' "
            f"  AND embedding_status IN ({placeholders}) "
            f"ORDER BY FIELD(embedding_status, 'failed', 'stale', 'pending'), "
            f"         published_date DESC "
            f"LIMIT %s",
            (*CLAIMABLE_STATUSES, batch_size),
            fetch=True,
        ) or []

        stats = {"total": len(rows), "success": 0, "failed": 0}
        for row in rows:
            ok = self.rebuild_article(row["id"])
            if ok:
                stats["success"] += 1
            else:
                stats["failed"] += 1

        logger.info(
            "index_pending: {s}/{t} indexed, {f} failed",
            s=stats["success"],
            t=stats["total"],
            f=stats["failed"],
        )
        return stats

    # ── Private state-machine transitions ─────────────────────────────

    def _claim(self, article_id: int) -> bool:
        """
        Atomically transition any claimable status → indexing for a single
        row. Returns True if this caller now owns the row.
        """
        placeholders = ",".join(["%s"] * len(CLAIMABLE_STATUSES))
        with db_connection(get_tracking_db_path()) as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE crawled_articles SET embedding_status=%s "
                f"WHERE id=%s AND embedding_enabled=1 "
                f"  AND embedding_status IN ({placeholders})",
                (STATUS_INDEXING, article_id, *CLAIMABLE_STATUSES),
            )
            conn.commit()
            return cur.rowcount > 0

    def _mark_indexed(self, article_id: int, content_hash: str) -> None:
        with db_connection(get_tracking_db_path()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE crawled_articles "
                "SET embedding_status=%s, indexed_hash=%s, last_indexed_at=%s "
                "WHERE id=%s",
                (STATUS_INDEXED, content_hash, datetime.now(), article_id),
            )
            conn.commit()

    def _mark_failed(self, article_id: int) -> None:
        with db_connection(get_tracking_db_path()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE crawled_articles SET embedding_status=%s WHERE id=%s",
                (STATUS_FAILED, article_id),
            )
            conn.commit()


# Module-level singleton (project convention)
vector_index_service = VectorIndexService()
