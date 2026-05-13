from agno.agent import Agent
import os
import traceback
from dotenv import load_dotenv
from services.celery_app import app, SessionLockedTask
from agno.storage.singlestore import SingleStoreStorage
from db.agent_config_v2 import (
    AGENT_DESCRIPTION,
    AGENT_INSTRUCTIONS,
    INITIAL_SESSION_STATE,
)
from services.model_router import router
from agents.search_agent import search_agent_run
from agents.scrape_agent import scrape_agent_run
from agents.script_agent import podcast_script_agent_run
from tools.ui_manager import ui_manager_run
from tools.user_source_selection import user_source_selection_run
from tools.session_state_manager import update_language, update_chat_title, mark_session_finished
from agents.image_generate_agent import image_generation_agent_run
from agents.audio_generate_agent import audio_generate_agent_run
from memory.manager import MemoryManager
import json

load_dotenv()

def _get_mysql_db_url() -> str:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url.startswith(("mysql://", "mysql+pymysql://")):
        raise RuntimeError("DATABASE_URL must be a MySQL URL (mysql:// or mysql+pymysql://)")
    # SQLAlchemy expects mysql+pymysql for PyMySQL
    if db_url.startswith("mysql://"):
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
    return db_url


def _get_mysql_db_name() -> str:
    """Extract database name from DATABASE_URL for use as agno storage schema."""
    from urllib.parse import urlparse
    db_url = os.environ.get("DATABASE_URL", "")
    return urlparse(db_url).path.lstrip("/")


@app.task(bind=True, max_retries=0, base=SessionLockedTask)
def agent_chat(self, session_id, message):
    try:
        print(f"Processing message for session {session_id}: {message[:50]}...")
        from services.internal_session_service import SessionService

        session_state = SessionService.get_session(session_id).get("state", INITIAL_SESSION_STATE)

        # ── Multi-layer memory: prepare augmented context ────────────
        # Retrieves conversation summary, user preferences, and content
        # history to inject into the agent's instructions.
        agno_storage = SingleStoreStorage(
            table_name="podcast_sessions",
            schema=_get_mysql_db_name(),
            db_url=_get_mysql_db_url(),
        )

        # Load existing messages from Agno storage for summarization
        existing_messages = []
        try:
            stored_session = agno_storage.read(session_id=session_id)
            if stored_session and hasattr(stored_session, "memory") and stored_session.memory:
                # Extract messages from Agno's stored memory
                if hasattr(stored_session.memory, "messages"):
                    existing_messages = [
                        {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}
                        for m in stored_session.memory.messages
                        if hasattr(m, "content") and m.content
                    ]
        except Exception:
            pass  # First message — no history yet

        memory_context = MemoryManager.prepare_context(
            session_id=session_id,
            user_id=session_id,  # Use session_id as user_id until auth is added
            instructions=AGENT_INSTRUCTIONS,
            current_messages=existing_messages,
        )

        _agent = Agent(
            model=router.get_agno_model(),
            storage=agno_storage,
            add_history_to_messages=True,
            read_chat_history=True,
            add_state_in_messages=True,
            num_history_runs=memory_context["window_size"],
            instructions=memory_context["instructions"],
            description=AGENT_DESCRIPTION,
            session_state=session_state,
            session_id=session_id,
            tools=[
                search_agent_run,
                scrape_agent_run,
                ui_manager_run,
                user_source_selection_run,
                update_language,
                podcast_script_agent_run,
                image_generation_agent_run,
                audio_generate_agent_run,
                update_chat_title,
                mark_session_finished,
            ],
            markdown=True,
        )
        response = _agent.run(message, session_id=session_id)
        print(f"Response generated for session {session_id}")
        _agent.write_to_storage(session_id=session_id)

        # ── Post-conversation memory update (fire-and-forget) ────────
        try:
            # Collect all messages including the new turn
            all_messages = existing_messages + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": response.content or ""},
            ]
            MemoryManager.post_conversation_update(
                session_id=session_id,
                user_id=session_id,
                messages=all_messages,
            )
        except Exception as mem_err:
            print(f"Non-fatal: memory update failed for {session_id}: {mem_err}")
        session_state = SessionService.get_session(session_id).get("state", INITIAL_SESSION_STATE)
        return {
            "session_id": session_id,
            "response": response.content,
            "stage": _agent.session_state.get("stage", "unknown"),
            "session_state": json.dumps(session_state),
            "is_processing": False,
            "process_type": None,
        }
    except Exception as e:
        print(f"Error in agent_chat for session {session_id}: {str(e)}")
        print(traceback.format_exc())
        return {
            "session_id": session_id,
            "response": f"I'm sorry, I encountered an error: {str(e)}. Please try again.",
            "stage": "error",
            "session_state": "{}",
            "is_processing": False,
            "process_type": None,
        }
