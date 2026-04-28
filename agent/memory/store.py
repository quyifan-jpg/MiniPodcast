"""
Persistence layer for the memory system.

Manages two tables:
  - conversation_summaries: LLM-compressed conversation history
  - user_preferences: Cross-session user profile (JSON)

Uses the existing db_connection + Redis cache infrastructure.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from db.connection import db_connection, execute_query
from db.config import get_db_path
from services.redis_cache import build_cache_key, sync_redis_cache
from memory.config import memory_settings


# ═══════════════════════════════════════════════════════════════════════
# Table initialization
# ═══════════════════════════════════════════════════════════════════════

_tables_initialized = False


def ensure_tables():
    """Create memory tables if they don't exist. Idempotent."""
    global _tables_initialized
    if _tables_initialized:
        return

    db_path = get_db_path("internal_sessions_db")
    with db_connection(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id VARCHAR(255) NOT NULL,
                summary TEXT NOT NULL,
                turn_count INT NOT NULL DEFAULT 0,
                summarized_up_to INT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uq_session (session_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                preferences JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE KEY uq_user (user_id)
            )
        """)

        conn.commit()

    _tables_initialized = True
    logger.info("Memory tables initialized")


# ═══════════════════════════════════════════════════════════════════════
# Conversation Summary Store
# ═══════════════════════════════════════════════════════════════════════

def _summary_cache_key(session_id: str) -> str:
    return build_cache_key("memory", "summary", {"session_id": session_id})


def get_summary(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the conversation summary for a session.

    Returns dict with keys: summary, turn_count, summarized_up_to
    or None if no summary exists.
    """
    # Check Redis cache first
    cache_key = _summary_cache_key(session_id)
    cached = sync_redis_cache.get_json(cache_key)
    if isinstance(cached, dict):
        return cached

    ensure_tables()
    db_path = get_db_path("internal_sessions_db")

    result = execute_query(
        db_path,
        "SELECT summary, turn_count, summarized_up_to FROM conversation_summaries WHERE session_id = ?",
        (session_id,),
        fetch=True,
        fetch_one=True,
    )

    if result:
        sync_redis_cache.set_json(
            cache_key, result, memory_settings.summary_cache_ttl_s
        )
        return result

    return None


def save_summary(
    session_id: str,
    summary: str,
    turn_count: int,
    summarized_up_to: int,
) -> None:
    """Upsert the conversation summary for a session."""
    ensure_tables()
    db_path = get_db_path("internal_sessions_db")
    now = datetime.now().isoformat()

    # Try update first, then insert
    with db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM conversation_summaries WHERE session_id = ?",
            (session_id,),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """UPDATE conversation_summaries
                   SET summary = ?, turn_count = ?, summarized_up_to = ?, updated_at = ?
                   WHERE session_id = ?""",
                (summary, turn_count, summarized_up_to, now, session_id),
            )
        else:
            cursor.execute(
                """INSERT INTO conversation_summaries
                   (session_id, summary, turn_count, summarized_up_to, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, summary, turn_count, summarized_up_to, now, now),
            )
        conn.commit()

    # Invalidate cache
    sync_redis_cache.delete(_summary_cache_key(session_id))
    logger.debug(
        "Summary saved: session={s} turns={t} summarized_up_to={u}",
        s=session_id, t=turn_count, u=summarized_up_to,
    )


# ═══════════════════════════════════════════════════════════════════════
# User Preferences Store
# ═══════════════════════════════════════════════════════════════════════

def _prefs_cache_key(user_id: str) -> str:
    return build_cache_key("memory", "preferences", {"user_id": user_id})


def get_preferences(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Get user preferences.

    Returns dict of preferences or None if no preferences exist.
    """
    cache_key = _prefs_cache_key(user_id)
    cached = sync_redis_cache.get_json(cache_key)
    if isinstance(cached, dict):
        return cached

    ensure_tables()
    db_path = get_db_path("internal_sessions_db")

    result = execute_query(
        db_path,
        "SELECT preferences FROM user_preferences WHERE user_id = ?",
        (user_id,),
        fetch=True,
        fetch_one=True,
    )

    if result and result.get("preferences"):
        prefs = result["preferences"]
        if isinstance(prefs, str):
            prefs = json.loads(prefs)
        sync_redis_cache.set_json(
            cache_key, prefs, memory_settings.preferences_cache_ttl_s
        )
        return prefs

    return None


def save_preferences(user_id: str, preferences: Dict[str, Any]) -> None:
    """Upsert user preferences."""
    ensure_tables()
    db_path = get_db_path("internal_sessions_db")
    now = datetime.now().isoformat()
    prefs_json = json.dumps(preferences, ensure_ascii=False)

    with db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM user_preferences WHERE user_id = ?",
            (user_id,),
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """UPDATE user_preferences
                   SET preferences = ?, updated_at = ?
                   WHERE user_id = ?""",
                (prefs_json, now, user_id),
            )
        else:
            cursor.execute(
                """INSERT INTO user_preferences
                   (user_id, preferences, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (user_id, prefs_json, now, now),
            )
        conn.commit()

    sync_redis_cache.delete(_prefs_cache_key(user_id))
    logger.debug("Preferences saved for user={u}", u=user_id)


# ═══════════════════════════════════════════════════════════════════════
# Content History (Layer 4) — reads existing podcasts table
# ═══════════════════════════════════════════════════════════════════════

def get_recent_podcasts(
    max_items: int = 5,
    days_back: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch recent podcast titles, dates, and source URLs from the
    existing podcasts table. No new table needed.
    """
    db_path = get_db_path("podcasts_db")
    try:
        results = execute_query(
            db_path,
            """SELECT id, title, date, language_code, sources_json, created_at
               FROM podcasts
               WHERE created_at >= DATE_SUB(NOW(), INTERVAL ? DAY)
               ORDER BY created_at DESC
               LIMIT ?""",
            (days_back, max_items),
            fetch=True,
        )

        podcasts = []
        for row in (results or []):
            sources = []
            if row.get("sources_json"):
                try:
                    sources = json.loads(row["sources_json"]) if isinstance(row["sources_json"], str) else row["sources_json"]
                except (json.JSONDecodeError, TypeError):
                    sources = []

            podcasts.append({
                "id": row.get("id"),
                "title": row.get("title", ""),
                "date": row.get("date", ""),
                "language": row.get("language_code", "en"),
                "source_count": len(sources) if isinstance(sources, list) else 0,
                "source_urls": [
                    s.get("url", "") for s in (sources if isinstance(sources, list) else [])
                ][:5],  # Limit to 5 URLs to save context
            })
        return podcasts

    except Exception as e:
        logger.warning("Failed to fetch podcast history: {e}", e=e)
        return []
