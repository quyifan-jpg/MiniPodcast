"""
Article-level embedding processor — thin CLI wrapper.

Behavior change vs. legacy:
  Old: scan crawled_articles, skip those already present in Milvus.
  New: drive off the embedding_status state machine. Picks up rows in
       pending / stale / failed and runs the unified rebuild (article
       vector + chunk vectors + MySQL chunk rows) atomically per row.

The actual logic now lives in `services.vector_index_service`. This file
exists only as a back-compat CLI entry point and as a single-process
fallback when the Celery fast-path is unavailable.

Run:
    python -m processors.embedding_processor --batch_size 20
"""

from __future__ import annotations

import argparse

from loguru import logger

from services.vector_index_service import vector_index_service


def process_articles_for_embedding(batch_size: int = 20) -> dict:
    """Single batch — picks up pending / stale / failed articles."""
    return vector_index_service.index_pending(batch_size=batch_size)


def process_in_batches(batch_size: int = 20, total_batches: int = 3) -> dict:
    """Run multiple batches in sequence; stop early when nothing claimable."""
    total = {"total": 0, "success": 0, "failed": 0}
    for i in range(total_batches):
        logger.info("Batch {n}/{m}", n=i + 1, m=total_batches)
        stats = vector_index_service.index_pending(batch_size=batch_size)
        for k in total:
            total[k] += stats.get(k, 0)
        if stats["total"] == 0:
            logger.info("No more claimable articles; stopping.")
            break
    return total


def print_stats(stats: dict) -> None:
    logger.info(
        "Embedding statistics — total={t} success={s} failed={f}",
        t=stats["total"], s=stats["success"], f=stats["failed"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Drive vector index sync via the state machine"
    )
    parser.add_argument("--batch_size", type=int, default=20)
    parser.add_argument("--total_batches", type=int, default=3)
    args = parser.parse_args()

    stats = process_in_batches(
        batch_size=args.batch_size,
        total_batches=args.total_batches,
    )
    print_stats(stats)
