"""
Search channel interface — the strategy pattern contract.

Equivalent to CAgent's SearchChannel interface.
Add a new retrieval source by:
  1. Implement this interface
  2. Register the instance in rag/factory.py
  3. Done — the engine discovers it automatically
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from rag.models import ChannelResult, ChannelType, SearchContext


class SearchChannel(ABC):
    """
    Abstract base for all retrieval channels.

    Each channel:
    - Has a type and priority (for dedup ordering)
    - Can decide at runtime whether it's enabled
    - Returns a ChannelResult containing ranked chunks
    """

    @abstractmethod
    def channel_type(self) -> ChannelType:
        """Which channel type this implementation represents."""
        ...

    @abstractmethod
    def priority(self) -> int:
        """
        Lower number = higher priority.

        Used for:
        - Deduplication: when the same document appears in multiple
          channels, the higher-priority channel's version is kept
        - Logging: channels are logged in priority order
        """
        ...

    def is_enabled(self, context: SearchContext) -> bool:
        """
        Runtime gate — return False to skip this channel for a given query.

        Override to implement conditional logic, e.g.:
        - Disable external search for internal-only queries
        - Disable social media channel if no social DB configured

        Default: always enabled.
        """
        return True

    @abstractmethod
    async def search(self, context: SearchContext) -> ChannelResult:
        """
        Execute the search and return results.

        This method runs inside asyncio.gather(), so:
        - Use await for async I/O
        - For sync code (Milvus, DB queries), use asyncio.to_thread()
        - Never block the event loop
        """
        ...
