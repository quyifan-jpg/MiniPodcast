from typing import Optional, Dict, Any
from fastapi import HTTPException
import json
from datetime import datetime
from db.config import get_db_path
from db.agent_config_v2 import INITIAL_SESSION_STATE
from contextlib import contextmanager
from db.connection import db_connection as shared_db_connection
from services.redis_cache import build_cache_key, sync_redis_cache


@contextmanager
def get_db_connection(db_name: str):
    """Get a fresh database connection each time."""
    db_path = get_db_path(db_name)
    with shared_db_connection(db_path) as conn:
        yield conn


class SessionService:
    """Service for managing internal session operations."""

    _CACHE_TTL_SECONDS = 45
    _CACHE_MODULE = "session"
    _CACHE_ENDPOINT = "get_session"
    _PODCAST_STATUS_MODULE = "podcast"
    _PODCAST_STATUS_ENDPOINT = "status"

    @staticmethod
    def _session_cache_key(session_id: str) -> str:
        return build_cache_key(SessionService._CACHE_MODULE, SessionService._CACHE_ENDPOINT, {"session_id": session_id})

    @staticmethod
    def _podcast_status_cache_key(session_id: str) -> str:
        return build_cache_key(
            SessionService._PODCAST_STATUS_MODULE,
            SessionService._PODCAST_STATUS_ENDPOINT,
            {"session_id": session_id},
        )

    @staticmethod
    def get_session(session_id: str) -> Dict[str, Any]:
        cache_key = SessionService._session_cache_key(session_id)
        cached_session = sync_redis_cache.get_json(cache_key)
        if isinstance(cached_session, dict):
            return cached_session
        try:
            with get_db_connection("internal_sessions_db") as conn:
                cursor = conn.cursor()
                query = """
                SELECT session_id, state, created_at
                FROM session_state
                WHERE session_id = ?
                """
                cursor.execute(query, (session_id,))
                session = cursor.fetchone()
                if not session:
                    return SessionService._initialize_session(session_id)
                session_dict = dict(session)
                if session_dict.get("state"):
                    try:
                        session_dict["state"] = json.loads(session_dict["state"])
                    except json.JSONDecodeError:
                        session_dict["state"] = {}
                sync_redis_cache.set_json(cache_key, session_dict, SessionService._CACHE_TTL_SECONDS)
                return session_dict
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Error fetching session: {str(e)}")

    @staticmethod
    def _initialize_session(session_id: str) -> Dict[str, Any]:
        try:
            with get_db_connection("internal_sessions_db") as conn:
                cursor = conn.cursor()
                state_json = json.dumps(INITIAL_SESSION_STATE)
                insert_query = """
                INSERT INTO session_state (session_id, state, created_at)
                VALUES (?, ?, ?)
                """
                current_time = datetime.now().isoformat()
                cursor.execute(insert_query, (session_id, state_json, current_time))
                conn.commit()
                session_data = {"session_id": session_id, "state": INITIAL_SESSION_STATE, "created_at": current_time}
                sync_redis_cache.set_json(
                    SessionService._session_cache_key(session_id),
                    session_data,
                    SessionService._CACHE_TTL_SECONDS,
                )
                return session_data
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error initializing session: {str(e)}")

    @staticmethod
    def save_session(session_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        try:
            state_json = json.dumps(state)
            with get_db_connection("internal_sessions_db") as conn:
                cursor = conn.cursor()
                existing_query = "SELECT session_id FROM session_state WHERE session_id = ?"
                cursor.execute(existing_query, (session_id,))
                existing_session = cursor.fetchone()
                if existing_session:
                    update_query = "UPDATE session_state SET state = ? WHERE session_id = ?"
                    cursor.execute(update_query, (state_json, session_id))
                else:
                    insert_query = "INSERT INTO session_state (session_id, state, created_at) VALUES (?, ?, ?)"
                    current_time = datetime.now().isoformat()
                    cursor.execute(insert_query, (session_id, state_json, current_time))
                conn.commit()
            sync_redis_cache.delete(SessionService._session_cache_key(session_id))
            sync_redis_cache.delete(SessionService._podcast_status_cache_key(session_id))
            return SessionService.get_session(session_id)
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Error saving session: {str(e)}")

    @staticmethod
    def delete_session(session_id: str) -> Dict[str, str]:
        try:
            with get_db_connection("internal_sessions_db") as conn:
                cursor = conn.cursor()
                existing_query = "SELECT session_id FROM session_state WHERE session_id = ?"
                cursor.execute(existing_query, (session_id,))
                existing_session = cursor.fetchone()
                if not existing_session:
                    raise HTTPException(status_code=404, detail="Session not found")
                delete_query = "DELETE FROM session_state WHERE session_id = ?"
                cursor.execute(delete_query, (session_id,))
                conn.commit()
                sync_redis_cache.delete(SessionService._session_cache_key(session_id))
                sync_redis_cache.delete(SessionService._podcast_status_cache_key(session_id))
                return {"message": f"Session with ID {session_id} successfully deleted"}
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Error deleting session: {str(e)}")

    @staticmethod
    def list_sessions(page: int = 1, per_page: int = 10, search: Optional[str] = None) -> Dict[str, Any]:
        try:
            with get_db_connection("internal_sessions_db") as conn:
                cursor = conn.cursor()
                offset = (page - 1) * per_page
                query_parts = [
                    "SELECT session_id, created_at",
                    "FROM session_state",
                ]
                query_params = []
                if search:
                    query_parts.append("WHERE session_id LIKE ?")
                    search_param = f"%{search}%"
                    query_params.append(search_param)

                count_query = " ".join(query_parts).replace(
                    "SELECT session_id, created_at",
                    "SELECT COUNT(*)",
                )
                cursor.execute(count_query, tuple(query_params))
                count_row = cursor.fetchone()
                if isinstance(count_row, dict):
                    total_count = list(count_row.values())[0] if count_row else 0
                else:
                    total_count = count_row[0] if count_row else 0
                query_parts.append("ORDER BY created_at DESC")
                query_parts.append("LIMIT ? OFFSET ?")
                query_params.extend([per_page, offset])
                sessions_query = " ".join(query_parts)
                cursor.execute(sessions_query, tuple(query_params))
                sessions = [dict(row) for row in cursor.fetchall()]
                total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 0
                has_next = page < total_pages
                has_prev = page > 1
                return {
                    "items": sessions,
                    "total": total_count,
                    "page": page,
                    "per_page": per_page,
                    "total_pages": total_pages,
                    "has_next": has_next,
                    "has_prev": has_prev,
                }
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=f"Error listing sessions: {str(e)}")
