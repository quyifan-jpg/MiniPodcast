"""
MemoryManager — orchestrates the 4-layer memory system.

This is the main entry point called from celery_tasks.py before
Agent creation. It:
  1. Determines the sliding window size (Layer 1)
  2. Generates/retrieves conversation summary (Layer 2)
  3. Loads user preferences (Layer 3)
  4. Fetches recent podcast history (Layer 4)
  5. Augments the agent instructions with all context

Equivalent to CAgent's ConversationMemoryPlanner which performs
token budgeting across memory layers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from memory.config import memory_settings
from memory import store
from memory.summarizer import summarize_conversation, extract_preferences


class MemoryManager:
    """
    Stateless orchestrator — all state lives in MySQL + Redis.

    Usage in celery_tasks.py:
        context = MemoryManager.prepare_context(
            session_id=session_id,
            user_id=user_id,
            instructions=AGENT_INSTRUCTIONS,
            current_messages=messages,       # from Agno storage
        )
        _agent = Agent(
            instructions=context["instructions"],
            num_history_runs=context["window_size"],
            ...
        )
    """

    @staticmethod
    def prepare_context(
        session_id: str,
        user_id: str = "",
        instructions: List[str] = None,
        current_messages: Optional[List[Dict[str, Any]]] = None,
        total_turns: int = 0,
    ) -> Dict[str, Any]:
        """
        Prepare the augmented context for Agent creation.

        Args:
            session_id: Current conversation session ID.
            user_id: User identifier for cross-session preferences.
                     Falls back to session_id if empty.
            instructions: Base AGENT_INSTRUCTIONS list to augment.
            current_messages: All messages in this session (from Agno storage).
                             Used for summarization of older turns.
            total_turns: Total number of turns in the conversation.
                        If 0, will be computed from current_messages.

        Returns:
            Dict with keys:
              - instructions: Augmented instructions list
              - window_size: Number of recent turns to keep (num_history_runs)
              - summary: The conversation summary (or "")
              - preferences: User preferences dict (or {})
              - history: Recent podcast list (or [])
        """
        store.ensure_tables()

        if instructions is None:
            instructions = []

        user_id = user_id or session_id
        messages = current_messages or []
        if total_turns <= 0:
            total_turns = _count_turns(messages)

        result = {
            "instructions": list(instructions),  # copy
            "window_size": memory_settings.window_size,
            "summary": "",
            "preferences": {},
            "history": [],
        }

        # ── Layer 2: Summary Memory ─────────────────────────────────
        if memory_settings.summary_enabled:
            summary_text = _get_or_create_summary(session_id, messages, total_turns)
            if summary_text:
                result["summary"] = summary_text
                result["instructions"] = _inject_section(
                    result["instructions"],
                    "对话摘要 / Conversation Summary",
                    summary_text,
                )

        # ── Layer 3: User Preferences ───────────────────────────────
        if memory_settings.preferences_enabled and user_id:
            prefs = store.get_preferences(user_id)
            if prefs:
                result["preferences"] = prefs
                prefs_text = _format_preferences(prefs)
                result["instructions"] = _inject_section(
                    result["instructions"],
                    "用户偏好 / User Preferences",
                    prefs_text,
                )

        # ── Layer 4: Content History ────────────────────────────────
        if memory_settings.history_enabled:
            podcasts = store.get_recent_podcasts(
                max_items=memory_settings.history_max_items,
                days_back=memory_settings.history_days_back,
            )
            if podcasts:
                result["history"] = podcasts
                history_text = _format_history(podcasts)
                result["instructions"] = _inject_section(
                    result["instructions"],
                    "近期播客历史 / Recent Podcast History",
                    history_text,
                )

        logger.info(
            "Memory context prepared: session={s} summary={has_s} prefs={has_p} history={h}",
            s=session_id,
            has_s=bool(result["summary"]),
            has_p=bool(result["preferences"]),
            h=len(result["history"]),
        )
        return result

    @staticmethod
    def post_conversation_update(
        session_id: str,
        user_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Called after a conversation turn to update memory state.

        This handles:
        - Updating the conversation summary if needed
        - Extracting and saving user preferences

        Should be called in a fire-and-forget manner (non-blocking to response).
        """
        user_id = user_id or session_id
        messages = messages or []

        if not messages:
            return

        total_turns = _count_turns(messages)

        # Update summary if conversation is long enough
        if memory_settings.summary_enabled and total_turns > memory_settings.summary_trigger_threshold:
            try:
                _update_summary(session_id, messages, total_turns)
            except Exception as e:
                logger.warning("Post-conversation summary update failed: {e}", e=e)

        # Extract preferences periodically (every 5 turns)
        if memory_settings.preferences_enabled and total_turns % 5 == 0:
            try:
                existing_prefs = store.get_preferences(user_id) or {}
                # Only use recent messages for preference extraction
                recent = messages[-10:] if len(messages) > 10 else messages
                new_prefs = extract_preferences(recent, existing_prefs)
                if new_prefs and new_prefs != existing_prefs:
                    store.save_preferences(user_id, new_prefs)
            except Exception as e:
                logger.warning("Preference extraction failed: {e}", e=e)


# ═══════════════════════════════════════════════════════════════════════
# Private helpers
# ═══════════════════════════════════════════════════════════════════════


def _count_turns(messages: List[Dict[str, Any]]) -> int:
    """Count user message turns in the conversation."""
    return sum(1 for m in messages if m.get("role") == "user")


def _get_or_create_summary(
    session_id: str,
    messages: List[Dict[str, Any]],
    total_turns: int,
) -> str:
    """
    Retrieve existing summary or create one if the conversation
    is long enough.
    """
    # Not enough turns to need a summary
    if total_turns <= memory_settings.summary_trigger_threshold:
        return ""

    # Check for existing summary
    existing = store.get_summary(session_id)
    if existing and existing.get("summarized_up_to", 0) >= total_turns - memory_settings.window_size:
        # Summary is fresh enough
        return existing.get("summary", "")

    # Need to create/update summary
    return _update_summary(session_id, messages, total_turns)


def _update_summary(
    session_id: str,
    messages: List[Dict[str, Any]],
    total_turns: int,
) -> str:
    """Create or update the conversation summary."""
    window = memory_settings.window_size

    # Messages outside the sliding window need summarization
    # We summarize everything except the last `window * 2` messages
    # (window * 2 because each turn has user + assistant)
    cutoff = max(0, len(messages) - window * 2)
    if cutoff <= 0:
        return ""

    older_messages = messages[:cutoff]

    # Get existing summary for incremental merge
    existing = store.get_summary(session_id)
    existing_summary = existing.get("summary", "") if existing else ""
    previously_summarized = existing.get("summarized_up_to", 0) if existing else 0

    # Only summarize messages that haven't been summarized yet
    if previously_summarized > 0:
        # Skip messages that were already summarized
        # Estimate: each turn ≈ 2 messages (user + assistant)
        skip_count = previously_summarized * 2
        new_messages = older_messages[skip_count:]
        if not new_messages:
            return existing_summary
    else:
        new_messages = older_messages

    summary = summarize_conversation(
        messages=new_messages,
        existing_summary=existing_summary,
    )

    if summary:
        summarized_up_to = total_turns - window
        store.save_summary(
            session_id=session_id,
            summary=summary,
            turn_count=total_turns,
            summarized_up_to=summarized_up_to,
        )

    return summary


def _inject_section(
    instructions: List[str],
    section_title: str,
    content: str,
) -> List[str]:
    """
    Inject a new section into the instructions list.

    Adds it before the "APPENDIX:" section if it exists,
    otherwise appends at the end.
    """
    section = f"\n## {section_title}\n{content}"

    # Find APPENDIX position
    for i, inst in enumerate(instructions):
        if isinstance(inst, str) and inst.strip().startswith("APPENDIX"):
            return instructions[:i] + [section] + instructions[i:]

    # No APPENDIX found — append at end
    return instructions + [section]


def _format_preferences(prefs: Dict[str, Any]) -> str:
    """Format user preferences dict into readable text for the agent."""
    lines = []

    if prefs.get("preferred_language"):
        lines.append(f"- Preferred language: {prefs['preferred_language']}")

    if prefs.get("topics_of_interest"):
        topics = prefs["topics_of_interest"]
        if isinstance(topics, list):
            lines.append(f"- Interested topics: {', '.join(topics)}")

    if prefs.get("style_preferences"):
        styles = prefs["style_preferences"]
        if isinstance(styles, list):
            lines.append(f"- Style: {', '.join(styles)}")

    if prefs.get("other_notes"):
        lines.append(f"- Notes: {prefs['other_notes']}")

    return "\n".join(lines) if lines else "No specific preferences recorded."


def _format_history(podcasts: List[Dict[str, Any]]) -> str:
    """Format recent podcast history into readable text."""
    if not podcasts:
        return "No recent podcasts."

    lines = ["Recent podcasts (avoid repeating the same topics):"]
    for p in podcasts:
        title = p.get("title", "Untitled")
        date = p.get("date", "")
        lang = p.get("language", "en")
        sources = p.get("source_count", 0)
        lines.append(f'- [{date}] "{title}" ({lang}, {sources} sources)')

    return "\n".join(lines)
