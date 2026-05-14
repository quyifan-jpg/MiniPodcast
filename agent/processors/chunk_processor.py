"""
Chunk-level RAG processor.

Pipeline:
  crawled_articles → split into chunks → embed each chunk
                   → store in article_chunks table
                   → build chunk-level FAISS index

Run directly:
  python -m processors.chunk_processor
"""

import os
import time
import argparse
import numpy as np
import faiss
from openai import OpenAI
from db.config import get_tracking_db_path, get_chunk_faiss_db_path
from db.connection import db_connection, execute_query
from utils.load_api_keys import load_api_key

EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_SIZE = 300  # words per chunk
CHUNK_OVERLAP = 50  # overlap between consecutive chunks


# ─────────────────────────────────────────
# Chunking
# ─────────────────────────────────────────


def split_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-based chunks."""
    if not text or not text.strip():
        return []
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = end - overlap
    return chunks


def build_chunk_text(article: dict) -> list[str]:
    """
    Build chunks for an article.
    Priority: content → summary. Title is prepended to the first chunk.
    """
    title = article.get("title", "").strip()
    body = (article.get("content") or article.get("summary") or "").strip()

    if not body:
        return [title] if title else []

    # Prepend title only to the first chunk for relevance signal
    chunks = split_into_chunks(body)
    if chunks and title:
        chunks[0] = f"{title}\n\n{chunks[0]}"
    return chunks


# ─────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────


def create_chunks_table(db_path: str):
    with db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS article_chunks (
                id          INT AUTO_INCREMENT PRIMARY KEY,
                article_id  INT  NOT NULL,
                chunk_index INT  NOT NULL,
                chunk_text  TEXT NOT NULL,
                embedding   LONGBLOB,
                in_faiss_index INT DEFAULT 0,
                created_at  TEXT NOT NULL,
                INDEX idx_ac_article_id (article_id),
                INDEX idx_ac_in_faiss   (in_faiss_index)
            )
        """)
        conn.commit()


def get_articles_without_chunks(db_path: str, limit: int = 50) -> list[dict]:
    query = """
        SELECT ca.id, ca.title, ca.summary, ca.content
        FROM crawled_articles ca
        WHERE ca.processed = 1
          AND ca.ai_status = 'success'
          AND NOT EXISTS (
              SELECT 1 FROM article_chunks ac WHERE ac.article_id = ca.id
          )
        ORDER BY ca.published_date DESC
        LIMIT ?
    """
    return execute_query(db_path, query, (limit,), fetch=True)


def store_chunk(db_path: str, article_id: int, chunk_index: int, chunk_text: str, embedding: list[float]) -> int | None:
    """Insert a chunk row and return its id."""
    from datetime import datetime

    blob = np.array(embedding, dtype=np.float32).tobytes()
    query = """
        INSERT INTO article_chunks (article_id, chunk_index, chunk_text, embedding, in_faiss_index, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
    """
    try:
        row_id = execute_query(db_path, query, (article_id, chunk_index, chunk_text, blob, datetime.now().isoformat()))
        return row_id
    except Exception as e:
        print(f"  Error storing chunk {chunk_index} for article {article_id}: {e}")
        return None


# ─────────────────────────────────────────
# FAISS helpers (reused pattern from faiss_indexing_processor)
# ─────────────────────────────────────────


def _load_or_create_index(index_path: str, dimension: int) -> faiss.Index:
    if os.path.exists(index_path):
        try:
            idx = faiss.read_index(index_path)
            print(f"Loaded chunk FAISS index ({idx.ntotal} vectors) from {index_path}")
            return idx
        except Exception as e:
            print(f"Could not load existing index: {e}. Creating new one.")
    m = 32
    idx = faiss.IndexHNSWFlat(dimension, m)
    idx.hnsw.efConstruction = 100
    idx.hnsw.efSearch = 64
    return idx


def _save_index(index: faiss.Index, index_path: str):
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    tmp = index_path + ".tmp"
    faiss.write_index(index, tmp)
    os.replace(tmp, index_path)


def _load_id_map(mapping_path: str) -> list:
    if os.path.exists(mapping_path):
        try:
            return np.load(mapping_path).tolist()
        except Exception:
            pass
    return []


def _save_id_map(id_map: list, mapping_path: str):
    os.makedirs(os.path.dirname(mapping_path), exist_ok=True)
    np.save(mapping_path, np.array(id_map))


def build_chunk_faiss_index(db_path: str, index_path: str, mapping_path: str, batch_size: int = 500):
    """
    Fetch un-indexed chunks from DB, add to FAISS, mark as indexed.
    """
    query = """
        SELECT id, embedding
        FROM article_chunks
        WHERE in_faiss_index = 0 AND embedding IS NOT NULL
        LIMIT ?
    """
    rows = execute_query(db_path, query, (batch_size,), fetch=True)
    if not rows:
        print("No new chunk embeddings to index.")
        return 0

    # Detect dimension from first row
    first_emb = np.frombuffer(rows[0]["embedding"], dtype=np.float32)
    dimension = first_emb.shape[0]

    faiss_index = _load_or_create_index(index_path, dimension)
    id_map = _load_id_map(mapping_path)

    vectors, chunk_ids = [], []
    for row in rows:
        emb = np.frombuffer(row["embedding"], dtype=np.float32)
        if emb.shape[0] != dimension:
            continue
        vectors.append(emb)
        chunk_ids.append(row["id"])

    if not vectors:
        return 0

    faiss_index.add(np.vstack(vectors).astype(np.float32))
    id_map.extend(chunk_ids)

    _save_index(faiss_index, index_path)
    _save_id_map(id_map, mapping_path)

    # Mark as indexed
    with db_connection(db_path) as conn:
        cursor = conn.cursor()
        placeholders = ",".join(["?"] * len(chunk_ids))
        cursor.execute(
            f"UPDATE article_chunks SET in_faiss_index = 1 WHERE id IN ({placeholders})",
            chunk_ids,
        )
        conn.commit()

    print(f"Added {len(vectors)} chunk vectors to FAISS index. Total: {faiss_index.ntotal}")
    return len(vectors)


# ─────────────────────────────────────────
# Main processing loop
# ─────────────────────────────────────────


def process_articles_into_chunks(
    db_path: str | None = None,
    openai_api_key: str | None = None,
    batch_size: int = 50,
) -> dict:
    if db_path is None:
        db_path = get_tracking_db_path()
    if not openai_api_key:
        raise ValueError("OpenAI API key is required")

    create_chunks_table(db_path)
    client = OpenAI(api_key=openai_api_key)

    articles = get_articles_without_chunks(db_path, limit=batch_size)
    if not articles:
        print("No new articles to chunk.")
        return {"articles": 0, "chunks": 0, "errors": 0}

    stats = {"articles": len(articles), "chunks": 0, "errors": 0}

    for i, article in enumerate(articles):
        article_id = article["id"]
        chunks = build_chunk_text(article)
        if not chunks:
            print(f"  [{i + 1}/{len(articles)}] Article {article_id}: no content, skipping")
            continue

        print(f"  [{i + 1}/{len(articles)}] Article {article_id} → {len(chunks)} chunks")

        for chunk_index, chunk_text in enumerate(chunks):
            try:
                resp = client.embeddings.create(input=chunk_text, model=EMBEDDING_MODEL)
                embedding = resp.data[0].embedding
                store_chunk(db_path, article_id, chunk_index, chunk_text, embedding)
                stats["chunks"] += 1
            except Exception as e:
                print(f"    Error embedding chunk {chunk_index}: {e}")
                stats["errors"] += 1

        # Polite rate-limit pause between articles
        if i < len(articles) - 1:
            time.sleep(0.5)

    return stats


def run(batch_size: int = 50, index_batch_size: int = 500):
    api_key = load_api_key("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    db_path = get_tracking_db_path()
    index_path, mapping_path = get_chunk_faiss_db_path()

    print("=== Step 1: Chunk + embed articles ===")
    stats = process_articles_into_chunks(db_path, api_key, batch_size)
    print(f"Articles processed: {stats['articles']}, Chunks created: {stats['chunks']}, Errors: {stats['errors']}")

    print("\n=== Step 2: Build chunk FAISS index ===")
    added = build_chunk_faiss_index(db_path, index_path, mapping_path, index_batch_size)
    print(f"Indexed {added} new chunk vectors.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chunk articles and build chunk-level FAISS index")
    parser.add_argument("--batch_size", type=int, default=50, help="Articles per batch")
    parser.add_argument("--index_batch_size", type=int, default=500, help="Chunks per FAISS batch")
    args = parser.parse_args()
    run(args.batch_size, args.index_batch_size)
