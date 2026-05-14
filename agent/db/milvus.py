"""
Milvus connection and collection management.

Two collections:
  article_vectors — one vector per article (1536-dim)
  chunk_vectors   — one vector per chunk   (1536-dim)

Usage:
    from db.milvus import get_milvus, ARTICLE_COLLECTION, CHUNK_COLLECTION
    mv = get_milvus()
    mv.article.insert([...])
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from loguru import logger
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

ARTICLE_COLLECTION = "article_vectors"
CHUNK_COLLECTION = "chunk_vectors"
VECTOR_DIM = 1536  # text-embedding-3-small
INDEX_PARAMS = {
    "metric_type": "IP",  # Inner Product ≈ cosine similarity (on normalised vecs)
    "index_type": "HNSW",
    "params": {"M": 32, "efConstruction": 100},
}
SEARCH_PARAMS = {"metric_type": "IP", "params": {"ef": 64}}


# ── Schemas ───────────────────────────────────────────────────────────


def _article_schema() -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
            FieldSchema("title", DataType.VARCHAR, max_length=1024),
            FieldSchema("url", DataType.VARCHAR, max_length=2048),
            FieldSchema("summary", DataType.VARCHAR, max_length=4096),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        ],
        description="Article-level embeddings",
        enable_dynamic_field=False,
    )


def _chunk_schema() -> CollectionSchema:
    return CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=False),
            FieldSchema("article_id", DataType.INT64),
            FieldSchema("chunk_index", DataType.INT32),
            FieldSchema("chunk_text", DataType.VARCHAR, max_length=8192),
            FieldSchema("title", DataType.VARCHAR, max_length=1024),
            FieldSchema("url", DataType.VARCHAR, max_length=2048),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        ],
        description="Chunk-level embeddings for RAG",
        enable_dynamic_field=False,
    )


# ── Collection wrapper ────────────────────────────────────────────────


@dataclass
class MilvusClient:
    article: Collection
    chunk: Collection

    def insert_articles(self, rows: List[dict]) -> int:
        """Insert article vectors. rows must have keys matching schema fields."""
        if not rows:
            return 0
        data = {f: [r[f] for r in rows] for f in ("id", "title", "url", "summary", "vector")}
        self.article.insert(list(data.values()))
        self.article.flush()
        return len(rows)

    def insert_chunks(self, rows: List[dict]) -> int:
        """Insert chunk vectors. rows must have keys matching schema fields."""
        if not rows:
            return 0
        data = {f: [r[f] for r in rows] for f in ("id", "article_id", "chunk_index", "chunk_text", "title", "url", "vector")}
        self.chunk.insert(list(data.values()))
        self.chunk.flush()
        return len(rows)

    def article_exists(self, article_id: int) -> bool:
        res = self.article.query(f"id == {article_id}", output_fields=["id"], limit=1)
        return len(res) > 0

    def chunk_exists_for_article(self, article_id: int) -> bool:
        res = self.chunk.query(f"article_id == {article_id}", output_fields=["id"], limit=1)
        return len(res) > 0

    def search_articles(self, vector: List[float], top_k: int = 20) -> List[dict]:
        self.article.load()
        results = self.article.search(
            data=[vector],
            anns_field="vector",
            param=SEARCH_PARAMS,
            limit=top_k,
            output_fields=["id", "title", "url", "summary"],
        )
        out = []
        for hit in results[0]:
            out.append(
                {
                    "article_id": hit.entity.get("id"),
                    "title": hit.entity.get("title", ""),
                    "url": hit.entity.get("url", ""),
                    "summary": hit.entity.get("summary", ""),
                    "score": hit.score,  # IP score, higher = more similar
                }
            )
        return out

    def search_chunks(self, vector: List[float], top_k: int = 30) -> List[dict]:
        self.chunk.load()
        results = self.chunk.search(
            data=[vector],
            anns_field="vector",
            param=SEARCH_PARAMS,
            limit=top_k,
            output_fields=["id", "article_id", "chunk_index", "chunk_text", "title", "url"],
        )
        out = []
        for hit in results[0]:
            out.append(
                {
                    "chunk_id": hit.entity.get("id"),
                    "article_id": hit.entity.get("article_id"),
                    "chunk_index": hit.entity.get("chunk_index"),
                    "chunk_text": hit.entity.get("chunk_text", ""),
                    "title": hit.entity.get("title", ""),
                    "url": hit.entity.get("url", ""),
                    "score": hit.score,
                }
            )
        return out

    def delete_article(self, article_id: int):
        self.article.delete(f"id == {article_id}")
        self.chunk.delete(f"article_id == {article_id}")


# ── Bootstrap ─────────────────────────────────────────────────────────


def _ensure_collection(name: str, schema_fn) -> Collection:
    if not utility.has_collection(name):
        col = Collection(name=name, schema=schema_fn())
        col.create_index(field_name="vector", index_params=INDEX_PARAMS)
        logger.info("Created Milvus collection: {}", name)
    else:
        col = Collection(name)
        # Create index if missing (idempotent)
        if not col.has_index():
            col.create_index(field_name="vector", index_params=INDEX_PARAMS)
    col.load()
    return col


_client: MilvusClient | None = None


def get_milvus() -> MilvusClient:
    """Return singleton MilvusClient, connecting on first call."""
    global _client
    if _client is not None:
        return _client

    host = os.getenv("MILVUS_HOST", "localhost")
    port = os.getenv("MILVUS_PORT", "19530")

    connections.connect("default", host=host, port=port)
    logger.info("Connected to Milvus at {}:{}", host, port)

    article_col = _ensure_collection(ARTICLE_COLLECTION, _article_schema)
    chunk_col = _ensure_collection(CHUNK_COLLECTION, _chunk_schema)

    _client = MilvusClient(article=article_col, chunk=chunk_col)
    return _client
