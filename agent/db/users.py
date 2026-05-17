"""
User database operations.

Table schema (created on startup by services/db_init.py):

    CREATE TABLE IF NOT EXISTS users (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        email         VARCHAR(255) NOT NULL UNIQUE,
        username      VARCHAR(100) NOT NULL UNIQUE,
        hashed_password VARCHAR(255) NOT NULL,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP
    );
"""

from __future__ import annotations

from db.config import get_db_path
from db.connection import execute_query

_db = lambda: get_db_path("tracking_db")  # users live in the main tracking DB


# ── Reads ──────────────────────────────────────────────────────────────────────


def get_user_by_email(email: str) -> dict | None:
    """Return user row dict or None."""
    return execute_query(
        _db(),
        "SELECT id, email, username, hashed_password, is_active, created_at FROM users WHERE email = %s LIMIT 1",
        (email,),
        fetch_one=True,
    )


def get_user_by_id(user_id: int) -> dict | None:
    """Return user row dict or None (password excluded)."""
    return execute_query(
        _db(),
        "SELECT id, email, username, is_active, created_at FROM users WHERE id = %s LIMIT 1",
        (user_id,),
        fetch_one=True,
    )


def get_user_by_username(username: str) -> dict | None:
    return execute_query(
        _db(),
        "SELECT id, email, username, is_active, created_at FROM users WHERE username = %s LIMIT 1",
        (username,),
        fetch_one=True,
    )


# ── Writes ─────────────────────────────────────────────────────────────────────


def create_user(email: str, username: str, hashed_password: str) -> int:
    """
    Insert a new user row. Returns the new user's integer ID.
    Raises an IntegrityError (via DB) if email or username already exists.
    """
    return execute_query(
        _db(),
        "INSERT INTO users (email, username, hashed_password) VALUES (%s, %s, %s)",
        (email, username, hashed_password),
    )


def deactivate_user(user_id: int) -> None:
    execute_query(
        _db(),
        "UPDATE users SET is_active = FALSE WHERE id = %s",
        (user_id,),
    )


def update_user_username(user_id: int, username: str) -> None:
    execute_query(
        _db(),
        "UPDATE users SET username = %s WHERE id = %s",
        (username, user_id),
    )


def update_user_email(user_id: int, email: str) -> None:
    execute_query(
        _db(),
        "UPDATE users SET email = %s WHERE id = %s",
        (email, user_id),
    )


def update_user_password(user_id: int, hashed_password: str) -> None:
    execute_query(
        _db(),
        "UPDATE users SET hashed_password = %s WHERE id = %s",
        (hashed_password, user_id),
    )


def get_password_hash(user_id: int) -> str | None:
    """Return the stored bcrypt hash for change-password verification."""
    row = execute_query(
        _db(),
        "SELECT hashed_password FROM users WHERE id = %s LIMIT 1",
        (user_id,),
        fetch_one=True,
    )
    return row["hashed_password"] if row else None
