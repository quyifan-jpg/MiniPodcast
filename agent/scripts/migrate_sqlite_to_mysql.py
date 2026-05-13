#!/usr/bin/env python3
import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.connection import db_connection
from db.config import DEFAULT_DB_PATHS
from services.mysql_init import init_mysql_schema


TABLES_TO_COPY = [
    ("sources_db", ["sources", "categories", "source_categories", "source_feeds"]),
    ("tracking_db", ["feed_tracking", "feed_entries", "crawled_articles", "article_categories"]),
    ("podcasts_db", ["podcasts"]),
    ("tasks_db", ["tasks", "task_executions", "podcast_configs"]),
    ("internal_sessions_db", ["session_state"]),
    ("social_media_db", ["posts"]),
]


def _read_rows(sqlite_path, table):
    if not Path(sqlite_path).exists():
        return [], []
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description]
        return cols, [tuple(row[c] for c in cols) for row in rows]
    finally:
        conn.close()


def _insert_rows(table, cols, rows):
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    with db_connection("unused_for_mysql") as conn:
        cur = conn.cursor()
        cur.executemany(sql, rows)
        conn.commit()
    return len(rows)


def main():
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith(("mysql://", "mysql+pymysql://")):
        raise RuntimeError("Please set DATABASE_URL to a MySQL connection URL before running migration.")

    init_mysql_schema()
    total_inserted = 0
    for db_name, tables in TABLES_TO_COPY:
        sqlite_path = DEFAULT_DB_PATHS[db_name]
        for table in tables:
            cols, rows = _read_rows(sqlite_path, table)
            inserted = _insert_rows(table, cols, rows) if cols else 0
            total_inserted += inserted
            print(f"{db_name}.{table}: copied {inserted} rows")
    print(f"Migration completed. Total rows copied: {total_inserted}")


if __name__ == "__main__":
    main()
