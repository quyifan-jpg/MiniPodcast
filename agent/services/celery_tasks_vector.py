"""
Celery tasks for vector index synchronization.

These tasks are the fast path that keeps Milvus in lock-step with MySQL
right after AI analysis completes. The slow path (periodic scan via
`index_pending`) covers anything the fast path missed (Celery down,
transient failures, hand-edits, etc.).
"""

from __future__ import annotations

from loguru import logger

from services.celery_app import app


@app.task(
    name="services.celery_tasks_vector.embed_article",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def embed_article(self, article_id: int):
    """
    Single-article rebuild — dispatched right after AI analysis succeeds.

    Idempotent: if the article is already indexed with the current
    content_hash, the underlying service short-circuits and returns False.
    """
    try:
        from services.vector_index_service import vector_index_service

        return vector_index_service.rebuild_article(article_id)
    except Exception as e:
        logger.error(
            "embed_article failed for {id}: {e}",
            id=article_id,
            e=str(e),
        )
        raise self.retry(exc=e)


@app.task(
    name="services.celery_tasks_vector.index_pending_batch",
)
def index_pending_batch(batch_size: int = 50):
    """
    Periodic batch — picks up anything left in pending/stale/failed.

    Schedule this via Celery beat or the project scheduler so the index
    stays in sync even when fast-path dispatches are lost.
    """
    from services.vector_index_service import vector_index_service

    return vector_index_service.index_pending(batch_size=batch_size)
