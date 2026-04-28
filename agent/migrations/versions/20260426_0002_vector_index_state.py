"""Vector index state machine columns on crawled_articles.

Revision ID: 0002_vector_index_state
Revises: 0001_initial
Create Date: 2026-04-26

Adds the columns required for the "MySQL is the source of truth, Milvus is
replayable" sync model:

    embedding_enabled  TINYINT  — admin-level on/off; disabled rows are
                                  excluded from RAG and from re-indexing
    content_hash       CHAR(64) — SHA-256 of (title + content); refreshed
                                  on every successful AI analysis
    indexed_hash       CHAR(64) — content_hash captured at the moment of
                                  the last successful Milvus index. Drift
                                  (content_hash != indexed_hash) signals
                                  a stale article needing re-embedding
    last_indexed_at    DATETIME — wall-clock of last successful index

It also gives the pre-existing `embedding_status` column a default of
'pending' so rows created by inserts that don't set it explicitly still
enter the state machine cleanly, and backfills historical NULLs.

Idempotency: uses Python-side `inspect()` checks (portable to MySQL
versions older than 8.0.29 which lack `IF NOT EXISTS` on column DDL).

Why no `MODIFY COLUMN`: rebuilding the table re-validates every row
under the active sql_mode, which fails on legacy rows with zero-date
timestamps in unrelated columns (e.g. '0000-00-00' published_date).
`ALTER COLUMN ... SET DEFAULT` is metadata-only and avoids that.
NULLs never appear in practice once this migration runs: existing
NULLs are backfilled to 'pending' below, and new INSERTs without an
explicit value pick up the default.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "0002_vector_index_state"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "crawled_articles"
INDEX_NAME = "idx_ca_embedding_state"


def _column_exists(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _index_exists(table: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def upgrade() -> None:
    # 1. Make sure embedding_status exists, then give it a default so
    #    rows inserted by code paths that don't set it explicitly still
    #    enter the state machine in 'pending'.
    #
    #    We deliberately use `ALTER COLUMN ... SET DEFAULT` instead of
    #    `MODIFY COLUMN`. MODIFY rebuilds the table and re-validates
    #    every row under the active sql_mode — which fails on legacy
    #    rows with '0000-00-00' timestamps. SET DEFAULT is a metadata
    #    operation only and doesn't touch row data.
    #
    #    We also do NOT add a NOT NULL constraint here: existing rows
    #    have NULLs and tightening the column would re-trigger the same
    #    table-rebuild problem. The application layer treats NULL the
    #    same as 'pending' (see vector_index_service); the backfill
    #    below covers historical NULLs.
    if not _column_exists(TABLE, "embedding_status"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ADD COLUMN `embedding_status` VARCHAR(32) DEFAULT 'pending' "
            f"COMMENT 'pending|indexing|indexed|stale|failed|disabled'"
        )
    else:
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ALTER COLUMN `embedding_status` SET DEFAULT 'pending'"
        )

    # 2. Backfill historical NULLs so the state-machine query picks
    #    them up on the next index_pending run.
    op.execute(
        f"UPDATE `{TABLE}` "
        f"SET `embedding_status` = 'pending' "
        f"WHERE `embedding_status` IS NULL"
    )

    # 3. Add the rest of the state fields.
    if not _column_exists(TABLE, "embedding_enabled"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ADD COLUMN `embedding_enabled` TINYINT NOT NULL DEFAULT 1 "
            f"COMMENT '1=visible to RAG, 0=excluded by admin'"
        )
    if not _column_exists(TABLE, "content_hash"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ADD COLUMN `content_hash` CHAR(64) NULL "
            f"COMMENT 'SHA-256 of title+content; refreshed on AI analysis success'"
        )
    if not _column_exists(TABLE, "indexed_hash"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ADD COLUMN `indexed_hash` CHAR(64) NULL "
            f"COMMENT 'content_hash at the moment of last successful Milvus index'"
        )
    if not _column_exists(TABLE, "last_indexed_at"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ADD COLUMN `last_indexed_at` DATETIME NULL "
            f"COMMENT 'Timestamp of last successful Milvus index'"
        )

    # 4. Index the columns the state-machine query filters on.
    if not _index_exists(TABLE, INDEX_NAME):
        op.execute(
            f"CREATE INDEX `{INDEX_NAME}` ON `{TABLE}` "
            f"(`embedding_enabled`, `embedding_status`)"
        )


def downgrade() -> None:
    if _index_exists(TABLE, INDEX_NAME):
        op.execute(f"DROP INDEX `{INDEX_NAME}` ON `{TABLE}`")

    for col in ("last_indexed_at", "indexed_hash", "content_hash", "embedding_enabled"):
        if _column_exists(TABLE, col):
            op.execute(f"ALTER TABLE `{TABLE}` DROP COLUMN `{col}`")

    # Drop the default we set in upgrade(); column shape itself is
    # untouched (we never widened/narrowed it, so no MODIFY needed).
    if _column_exists(TABLE, "embedding_status"):
        op.execute(
            f"ALTER TABLE `{TABLE}` "
            f"ALTER COLUMN `embedding_status` DROP DEFAULT"
        )
