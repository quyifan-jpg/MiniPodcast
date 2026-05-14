import os
from typing import Dict, List, Any, Tuple, Union
from fastapi import HTTPException
from contextlib import contextmanager
from db.config import get_db_path
from db.connection import db_connection as shared_db_connection


@contextmanager
def db_connection(db_path: str):
    """Context manager for database connections."""
    database_url = os.environ.get("DATABASE_URL", "")
    using_mysql = database_url.startswith("mysql://") or database_url.startswith("mysql+pymysql://")
    if not using_mysql and not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail=f"Database {db_path} not found. Initialize the database first.")
    try:
        with shared_db_connection(db_path) as conn:
            yield conn
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


class DatabaseService:
    """Service for managing database connections and operations."""

    def __init__(self, db_name: str):
        """
        Initialize the database service.

        Args:
            db_name: Name of the database (sources_db, tracking_db, etc.)
        """
        self.db_path = get_db_path(db_name)

    async def execute_query(
        self, query: str, params: Tuple = (), fetch: bool = False, fetch_one: bool = False
    ) -> Union[List[Dict[str, Any]], Dict[str, Any], int]:
        """Execute a query with error handling for FastAPI."""
        try:
            with db_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)

                if fetch_one:
                    result = cursor.fetchone()
                    return dict(result) if result else None
                elif fetch:
                    return [dict(row) for row in cursor.fetchall()]
                else:
                    conn.commit()
                    return cursor.lastrowid
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    async def execute_write_many(self, query: str, params_list: List[Tuple]) -> int:
        """Execute multiple write operations in a single transaction."""
        try:
            with db_connection(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.executemany(query, params_list)
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


sources_db = DatabaseService(db_name="sources_db")
tracking_db = DatabaseService(db_name="tracking_db")
podcasts_db = DatabaseService(db_name="podcasts_db")
tasks_db = DatabaseService(db_name="tasks_db")
social_media_db = DatabaseService(db_name="social_media_db")
