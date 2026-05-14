"""Initial schema baseline + soft delete columns.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-15

Notes:
  This migration captures the existing schema from mysql_init.py as a baseline.
  New tables added: none (schema already created by db_init.py).
  New columns added:
    - deleted_at (TIMESTAMP NULL) on crawled_articles, sources, podcasts, tasks
  These enable soft delete via the SoftDeleteMixin pattern.

  Idempotency: uses Python-side `inspect()` checks instead of MySQL's
  `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, which are
  only available in MySQL 8.0.29+.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    # ── Add soft delete column to key tables ──────────────────────────────────
    # Equivalent to CAgent's MyBatis-Plus `deleted` field.
    for table in ["crawled_articles", "sources", "podcasts", "tasks"]:
        if not _column_exists(table, "deleted_at"):
            op.execute(
                f"ALTER TABLE `{table}` ADD COLUMN `deleted_at` TIMESTAMP NULL DEFAULT NULL COMMENT 'Soft delete timestamp. NULL = active record.'"
            )

        index_name = f"idx_{table}_deleted_at"
        if not _index_exists(table, index_name):
            op.execute(f"CREATE INDEX `{index_name}` ON `{table}` (`deleted_at`)")


def downgrade() -> None:
    for table in ["crawled_articles", "sources", "podcasts", "tasks"]:
        index_name = f"idx_{table}_deleted_at"
        if _index_exists(table, index_name):
            op.execute(f"DROP INDEX `{index_name}` ON `{table}`")
        if _column_exists(table, "deleted_at"):
            op.execute(f"ALTER TABLE `{table}` DROP COLUMN `deleted_at`")
