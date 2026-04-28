"""
Multi-layer memory management for the podcast agent.

Implements a 4-layer memory architecture inspired by CAgent's
ConversationMemoryStore + ConversationMemorySummaryService +
UserMemoryService + ConversationMemoryPlanner:

  Layer 1 — Sliding Window:  Recent N turns (full text, in-context)
  Layer 2 — Summary Memory:  LLM-compressed older conversations
  Layer 3 — User Preferences: Cross-session user profile (JSON)
  Layer 4 — Content History:  Recent podcasts & used articles

Integration point: celery_tasks.py → agent_chat() calls
MemoryManager.prepare_context() before Agent creation.
"""

from memory.manager import MemoryManager
from memory.config import memory_settings

__all__ = ["MemoryManager", "memory_settings"]
