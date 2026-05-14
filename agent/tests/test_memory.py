"""
Unit tests for the multi-layer memory management system.

Tests the MemoryManager orchestration, summarizer, store, and config
without requiring external dependencies (MySQL, Redis, OpenAI).

Run:
    cd agent && python -m pytest tests/test_memory.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from memory.config import MemorySettings
from memory.manager import (
    MemoryManager,
    _count_turns,
    _inject_section,
    _format_preferences,
    _format_history,
)
from memory.summarizer import _format_messages


# ═══════════════════════════════════════════════════════════════════════
# Test fixtures
# ═══════════════════════════════════════════════════════════════════════


def _make_messages(n_turns: int) -> list:
    """Create a conversation with n user turns (n*2 total messages)."""
    messages = []
    for i in range(n_turns):
        messages.append({"role": "user", "content": f"User message {i + 1}"})
        messages.append({"role": "assistant", "content": f"Assistant reply {i + 1}"})
    return messages


def _make_settings(**overrides) -> MemorySettings:
    """Create MemorySettings with test defaults."""
    defaults = {
        "window_size": 5,
        "summary_enabled": True,
        "summary_max_words": 200,
        "summary_trigger_threshold": 8,
        "preferences_enabled": True,
        "history_enabled": True,
        "history_max_items": 5,
        "history_days_back": 30,
    }
    defaults.update(overrides)
    return MemorySettings(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# Config tests
# ═══════════════════════════════════════════════════════════════════════


class TestConfig:
    def test_default_values(self):
        settings = MemorySettings()
        assert settings.window_size == 5
        assert settings.summary_enabled is True
        assert settings.summary_max_words == 200
        assert settings.summary_trigger_threshold == 8
        assert settings.preferences_enabled is True
        assert settings.history_enabled is True
        assert settings.history_max_items == 5

    def test_env_prefix(self):
        """Settings should use MEMORY_ prefix."""
        assert MemorySettings.model_config["env_prefix"] == "MEMORY_"


# ═══════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_count_turns(self):
        messages = _make_messages(5)
        assert _count_turns(messages) == 5

    def test_count_turns_empty(self):
        assert _count_turns([]) == 0

    def test_count_turns_only_user(self):
        messages = [{"role": "user", "content": "hi"}]
        assert _count_turns(messages) == 1

    def test_inject_section_before_appendix(self):
        instructions = [
            "Step 1",
            "Step 2",
            "APPENDIX:",
            "Appendix item 1",
        ]
        result = _inject_section(instructions, "Test Section", "Test content")
        # Section should be inserted before APPENDIX
        assert len(result) == 5
        assert "Test Section" in result[2]
        assert result[3] == "APPENDIX:"

    def test_inject_section_no_appendix(self):
        instructions = ["Step 1", "Step 2"]
        result = _inject_section(instructions, "Test Section", "content")
        assert len(result) == 3
        assert "Test Section" in result[2]

    def test_format_preferences_full(self):
        prefs = {
            "preferred_language": "Chinese",
            "topics_of_interest": ["AI", "Kubernetes"],
            "style_preferences": ["casual", "concise"],
            "other_notes": "Prefers short episodes",
        }
        text = _format_preferences(prefs)
        assert "Chinese" in text
        assert "AI" in text
        assert "casual" in text
        assert "short episodes" in text

    def test_format_preferences_empty(self):
        text = _format_preferences({})
        assert "No specific preferences" in text

    def test_format_preferences_partial(self):
        prefs = {"preferred_language": "English"}
        text = _format_preferences(prefs)
        assert "English" in text
        assert "topics" not in text.lower()

    def test_format_history(self):
        podcasts = [
            {"title": "AI Revolution", "date": "2026-04-15", "language": "en", "source_count": 3},
            {"title": "K8s Deep Dive", "date": "2026-04-10", "language": "zh", "source_count": 5},
        ]
        text = _format_history(podcasts)
        assert "AI Revolution" in text
        assert "K8s Deep Dive" in text
        assert "avoid repeating" in text

    def test_format_history_empty(self):
        text = _format_history([])
        assert "No recent podcasts" in text


# ═══════════════════════════════════════════════════════════════════════
# Summarizer helper tests
# ═══════════════════════════════════════════════════════════════════════


class TestSummarizerHelpers:
    def test_format_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        text = _format_messages(messages)
        assert "User: Hello" in text
        assert "Assistant: Hi there" in text

    def test_format_messages_truncates_long(self):
        messages = [{"role": "user", "content": "a" * 1000}]
        text = _format_messages(messages)
        assert len(text) < 1000
        assert "..." in text

    def test_format_messages_skips_empty(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "World"},
        ]
        text = _format_messages(messages)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        assert len(lines) == 2


# ═══════════════════════════════════════════════════════════════════════
# MemoryManager tests (with mocked store)
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryManager:
    @patch("memory.manager.store")
    def test_prepare_context_short_conversation(self, mock_store):
        """Short conversations should not trigger summarization."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = None
        mock_store.get_preferences.return_value = None
        mock_store.get_recent_podcasts.return_value = []

        messages = _make_messages(3)  # Only 3 turns, below threshold
        with patch("memory.manager.memory_settings", _make_settings()):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                instructions=["Step 1", "APPENDIX:", "Item"],
                current_messages=messages,
            )

        assert result["window_size"] == 5
        assert result["summary"] == ""
        assert result["preferences"] == {}
        assert result["history"] == []

    @patch("memory.manager.store")
    def test_prepare_context_with_preferences(self, mock_store):
        """User preferences should be injected into instructions."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = None
        mock_store.get_preferences.return_value = {
            "preferred_language": "Chinese",
            "topics_of_interest": ["AI"],
        }
        mock_store.get_recent_podcasts.return_value = []

        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False)):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                user_id="user-1",
                instructions=["Step 1"],
            )

        assert result["preferences"]["preferred_language"] == "Chinese"
        # Check that preferences section was injected
        full_text = " ".join(result["instructions"])
        assert "Chinese" in full_text
        assert "User Preferences" in full_text

    @patch("memory.manager.store")
    def test_prepare_context_with_history(self, mock_store):
        """Podcast history should be injected into instructions."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = None
        mock_store.get_preferences.return_value = None
        mock_store.get_recent_podcasts.return_value = [
            {"title": "AI Today", "date": "2026-04-15", "language": "en", "source_count": 3},
        ]

        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False, preferences_enabled=False)):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                instructions=["Step 1"],
            )

        assert len(result["history"]) == 1
        full_text = " ".join(result["instructions"])
        assert "AI Today" in full_text
        assert "Recent Podcast History" in full_text

    @patch("memory.manager.store")
    def test_prepare_context_with_existing_summary(self, mock_store):
        """Existing fresh summary should be used without re-summarizing."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = {
            "summary": "User wants a podcast about Kubernetes.",
            "turn_count": 10,
            "summarized_up_to": 7,
        }
        mock_store.get_preferences.return_value = None
        mock_store.get_recent_podcasts.return_value = []

        messages = _make_messages(10)  # 10 turns, above threshold

        with patch("memory.manager.memory_settings", _make_settings(preferences_enabled=False, history_enabled=False)):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                instructions=["Step 1"],
                current_messages=messages,
            )

        assert "Kubernetes" in result["summary"]
        full_text = " ".join(result["instructions"])
        assert "Conversation Summary" in full_text

    @patch("memory.manager.store")
    def test_prepare_context_all_disabled(self, mock_store):
        """With everything disabled, instructions should be unchanged."""
        mock_store.ensure_tables.return_value = None

        original = ["Step 1", "Step 2"]
        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False, preferences_enabled=False, history_enabled=False)):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                instructions=original,
            )

        assert result["instructions"] == original
        assert result["window_size"] == 5

    @patch("memory.manager.store")
    @patch("memory.manager.extract_preferences")
    def test_post_conversation_update_extracts_prefs(self, mock_extract, mock_store):
        """Preferences should be extracted every 5 turns."""
        mock_store.get_preferences.return_value = {}
        mock_store.get_summary.return_value = None
        mock_extract.return_value = {"preferred_language": "English"}

        messages = _make_messages(10)  # 10 turns, divisible by 5

        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False)):
            MemoryManager.post_conversation_update(
                session_id="test-session",
                user_id="user-1",
                messages=messages,
            )

        mock_extract.assert_called_once()
        mock_store.save_preferences.assert_called_once()

    @patch("memory.manager.store")
    def test_post_conversation_empty_messages(self, mock_store):
        """Empty messages should be a no-op."""
        with patch("memory.manager.memory_settings", _make_settings()):
            MemoryManager.post_conversation_update(
                session_id="test-session",
                messages=[],
            )

        mock_store.save_summary.assert_not_called()
        mock_store.save_preferences.assert_not_called()

    @patch("memory.manager.store")
    def test_prepare_context_instructions_none(self, mock_store):
        """None instructions should default to empty list."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = None
        mock_store.get_preferences.return_value = None
        mock_store.get_recent_podcasts.return_value = []

        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False, preferences_enabled=False, history_enabled=False)):
            result = MemoryManager.prepare_context(
                session_id="test-session",
                instructions=None,
            )

        assert isinstance(result["instructions"], list)

    @patch("memory.manager.store")
    def test_user_id_fallback_to_session_id(self, mock_store):
        """When user_id is empty, should use session_id."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = None
        mock_store.get_preferences.return_value = {"preferred_language": "English"}
        mock_store.get_recent_podcasts.return_value = []

        with patch("memory.manager.memory_settings", _make_settings(summary_enabled=False, history_enabled=False)):
            MemoryManager.prepare_context(
                session_id="session-123",
                user_id="",  # Empty
                instructions=["Step 1"],
            )

        # Should have called get_preferences with session_id as fallback
        mock_store.get_preferences.assert_called_with("session-123")


# ═══════════════════════════════════════════════════════════════════════
# Integration test — full pipeline with mocks
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryManagerIntegration:
    @patch("memory.manager.store")
    def test_full_augmented_instructions(self, mock_store):
        """All 3 augmented layers should appear in correct order."""
        mock_store.ensure_tables.return_value = None
        mock_store.get_summary.return_value = {
            "summary": "Discussed AI podcast topics.",
            "turn_count": 12,
            "summarized_up_to": 9,
        }
        mock_store.get_preferences.return_value = {
            "preferred_language": "Chinese",
            "topics_of_interest": ["AI", "Cloud"],
        }
        mock_store.get_recent_podcasts.return_value = [
            {"title": "AI News", "date": "2026-04-15", "language": "en", "source_count": 4},
        ]

        original_instructions = [
            "Step 1: Search",
            "Step 2: Scrape",
            "APPENDIX:",
            "Appendix item",
        ]

        messages = _make_messages(12)

        with patch("memory.manager.memory_settings", _make_settings()):
            result = MemoryManager.prepare_context(
                session_id="full-test",
                user_id="user-1",
                instructions=original_instructions,
                current_messages=messages,
            )

        instructions = result["instructions"]
        full_text = "\n".join(instructions)

        # All sections should be present
        assert "Conversation Summary" in full_text
        assert "User Preferences" in full_text
        assert "Recent Podcast History" in full_text
        assert "APPENDIX:" in full_text

        # Sections should appear before APPENDIX
        appendix_idx = next(i for i, inst in enumerate(instructions) if isinstance(inst, str) and "APPENDIX" in inst)

        for i, inst in enumerate(instructions):
            if isinstance(inst, str) and "Conversation Summary" in inst:
                assert i < appendix_idx
            if isinstance(inst, str) and "User Preferences" in inst:
                assert i < appendix_idx
            if isinstance(inst, str) and "Recent Podcast History" in inst:
                assert i < appendix_idx

        # Original instructions should still be there
        assert "Step 1: Search" in instructions
        assert "Step 2: Scrape" in instructions
