"""
Chunk-level RAG processor — thin CLI wrapper.

Deprecated as a standalone pipeline: chunking is now performed inside
`vector_index_service.rebuild_article` together with article-level
embedding, so MySQL `article_chunks` rows and Milvus `chunk_vectors`
entries are written atomically per article.

This module remains as a CLI shim so existing scheduler / Makefile
invocations continue to work. It simply delegates to `index_pending`,
which produces both article and chunk vectors in one pass.

Run:
    python -m processors.chunk_processor --batch_size 50
"""

from __future__ import annotations

import argparse

from loguru import logger

from services.vector_index_service import vector_index_service


def run(batch_size: int = 50) -> dict:
    logger.info("=== Vector index sync (chunks + article vectors) ===")
    stats = vector_index_service.index_pending(batch_size=batch_size)
    logger.info(
        "Done — total={t} success={s} failed={f}",
        t=stats["total"], s=stats["success"], f=stats["failed"],
    )
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Trigger article+chunk re-embedding for any pending/stale rows"
    )
    parser.add_argument("--batch_size", type=int, default=50)
    args = parser.parse_args()
    run(args.batch_size)
