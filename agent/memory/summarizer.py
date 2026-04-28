"""
LLM-based conversation summarizer.

Compresses older conversation turns into a concise summary that
preserves key decisions, user preferences, and context needed
for continuity.

Equivalent to CAgent's ConversationMemorySummaryService which
uses async LLM compression with token budgeting.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from memory.config import memory_settings


SUMMARY_SYSTEM_PROMPT = """\
You are a conversation summarizer for a podcast production assistant.

Given a conversation history, produce a concise summary that captures:
1. The user's topic/intent and any clarifications
2. Key decisions made (selected sources, language preference, style choices)
3. Current progress in the workflow (search → scrape → script → image → audio)
4. Any specific user requests or constraints mentioned

Rules:
- Write in third person ("The user wants...", "The assistant found...")
- Keep under {max_words} words
- Focus on information needed for conversation continuity
- Do NOT include greetings or small talk
- If there's an existing summary, merge the new information with it
"""

MERGE_PROMPT = """\
Existing summary:
{existing_summary}

New conversation turns to incorporate:
{new_turns}

Merge the new information into the existing summary. Keep under {max_words} words.
Return ONLY the merged summary, no explanation.
"""

INITIAL_PROMPT = """\
Conversation to summarize:
{conversation}

Return ONLY the summary, no explanation. Keep under {max_words} words.
"""


def summarize_conversation(
    messages: List[Dict[str, Any]],
    existing_summary: Optional[str] = None,
    max_words: int = 0,
) -> str:
    """
    Synchronously summarize conversation messages using LLM.

    Args:
        messages: List of message dicts with 'role' and 'content' keys.
        existing_summary: Previous summary to merge with (incremental mode).
        max_words: Target word count. Defaults to memory_settings.summary_max_words.

    Returns:
        Summarized text string.
    """
    if not messages:
        return existing_summary or ""

    if max_words <= 0:
        max_words = memory_settings.summary_max_words

    try:
        from services.model_router import router

        llm = router.get_chat_model()

        system_msg = SUMMARY_SYSTEM_PROMPT.format(max_words=max_words)

        # Format conversation turns
        formatted_turns = _format_messages(messages)

        if existing_summary:
            user_msg = MERGE_PROMPT.format(
                existing_summary=existing_summary,
                new_turns=formatted_turns,
                max_words=max_words,
            )
        else:
            user_msg = INITIAL_PROMPT.format(
                conversation=formatted_turns,
                max_words=max_words,
            )

        response = llm.invoke([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ])

        summary = response.content.strip()
        logger.info(
            "Conversation summarized: {n} messages → {w} words",
            n=len(messages),
            w=len(summary.split()),
        )
        return summary

    except Exception as e:
        logger.warning("Summarization failed, returning existing: {e}", e=e)
        return existing_summary or ""


def extract_preferences(
    messages: List[Dict[str, Any]],
    existing_preferences: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract user preferences from conversation messages using LLM.

    Looks for patterns like:
    - Language preferences ("I prefer Chinese podcasts")
    - Topic interests ("I'm interested in AI and tech")
    - Style preferences ("Keep it casual", "More detailed")
    - Duration preferences ("Short episodes please")

    Args:
        messages: Conversation messages to analyze.
        existing_preferences: Previously extracted preferences to merge with.

    Returns:
        Updated preferences dict.
    """
    if not messages:
        return existing_preferences or {}

    try:
        from services.model_router import router

        llm = router.get_chat_model()

        formatted = _format_messages(messages)
        existing_str = ""
        if existing_preferences:
            import json
            existing_str = f"\nExisting preferences:\n{json.dumps(existing_preferences, indent=2, ensure_ascii=False)}\n"

        response = llm.invoke([
            {
                "role": "system",
                "content": (
                    "You extract user preferences from podcast assistant conversations.\n"
                    "Return a JSON object with these optional keys:\n"
                    '  "preferred_language": string (language name),\n'
                    '  "topics_of_interest": [string] (topics they care about),\n'
                    '  "style_preferences": [string] (casual/formal/detailed/concise),\n'
                    '  "other_notes": string (anything else relevant)\n\n'
                    "If a preference was already known (see existing preferences) and "
                    "hasn't changed, keep it. If no preferences are detectable, "
                    "return the existing preferences unchanged.\n"
                    "Return ONLY valid JSON, no explanation."
                ),
            },
            {
                "role": "user",
                "content": f"{existing_str}\nConversation:\n{formatted}",
            },
        ])

        import json
        try:
            prefs = json.loads(response.content.strip())
            if not isinstance(prefs, dict):
                return existing_preferences or {}
            # Merge with existing
            merged = {**(existing_preferences or {}), **prefs}
            # Remove empty values
            merged = {k: v for k, v in merged.items() if v}
            logger.debug("Preferences extracted: {p}", p=list(merged.keys()))
            return merged
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON preferences, keeping existing")
            return existing_preferences or {}

    except Exception as e:
        logger.warning("Preference extraction failed: {e}", e=e)
        return existing_preferences or {}


# ── Private helpers ──────────────────────────────────────────────────

def _format_messages(messages: List[Dict[str, Any]]) -> str:
    """Format message list into readable conversation text."""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not content:
            continue
        # Truncate very long messages
        if len(content) > 500:
            content = content[:500] + "..."
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)
