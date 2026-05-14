"""
Multi-channel retrieval engine — the orchestrator.

Equivalent to CAgent's MultiChannelRetrievalEngine.

Two-phase execution:
  Phase 1: Run all enabled search channels in parallel (asyncio.gather)
  Phase 2: Run post-processors sequentially (chain of responsibility)

Design principles:
  - Single channel failure never breaks the entire pipeline
  - Post-processors are composable and order-independent (except by order())
  - The engine is stateless — all state flows through SearchContext
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from rag.channels.base import SearchChannel
from rag.models import ChannelResult, RetrievedChunk, SearchContext
from rag.postprocessors.base import PostProcessor


class MultiChannelRetrievalEngine:
    """
    Orchestrates multi-channel retrieval with post-processing pipeline.

    Usage:
        engine = MultiChannelRetrievalEngine(
            channels=[ChunkVectorChannel(), KeywordChannel(), ...],
            postprocessors=[DeduplicationProcessor(), RRFProcessor(), ...],
        )
        results = await engine.retrieve(SearchContext(original_query="..."))
    """

    def __init__(
        self,
        channels: list[SearchChannel],
        postprocessors: list[PostProcessor],
        *,
        channel_timeout_s: float = 30.0,
    ):
        """
        Args:
            channels: List of search channels (sorted by priority internally).
            postprocessors: List of post-processors (sorted by order internally).
            channel_timeout_s: Per-channel timeout in seconds.
        """
        self._channels = sorted(channels, key=lambda c: c.priority())
        self._postprocessors = sorted(postprocessors, key=lambda p: p.order())
        self._channel_timeout_s = channel_timeout_s

    async def retrieve(self, context: SearchContext) -> list[RetrievedChunk]:
        """
        Main entry point — execute full retrieval pipeline.

        Returns a ranked, deduplicated, quality-filtered list of chunks
        ready for downstream consumption (script generation, etc.).
        """
        total_start = time.perf_counter()

        # ── Phase 1: Parallel channel execution ──────────────────────
        channel_results = await self._execute_channels(context)

        # Merge all channel chunks into a single list
        merged: list[RetrievedChunk] = []
        for result in channel_results:
            merged.extend(result.chunks)

        logger.info(
            "Phase 1 complete: {total} chunks from {channels} channel(s)",
            total=len(merged),
            channels=len(channel_results),
        )

        if not merged:
            logger.warning("No results from any channel")
            return []

        # ── Phase 2: Sequential post-processing ─────────────────────
        processed = merged
        for processor in self._postprocessors:
            if not processor.is_enabled(context):
                logger.debug(
                    "Skipping {name} (disabled)",
                    name=type(processor).__name__,
                )
                continue

            before = len(processed)
            processed = processor.process(processed, channel_results, context)
            logger.info(
                "{name}: {before} → {after} chunks",
                name=type(processor).__name__,
                before=before,
                after=len(processed),
            )

        total_ms = (time.perf_counter() - total_start) * 1000
        logger.info(
            "Retrieval complete: {count} final chunks in {ms:.0f}ms",
            count=len(processed),
            ms=total_ms,
        )

        return processed

    async def _execute_channels(self, context: SearchContext) -> list[ChannelResult]:
        """Run all enabled channels in parallel with per-channel fault isolation."""
        enabled = [ch for ch in self._channels if ch.is_enabled(context)]

        if not enabled:
            logger.warning("No search channels enabled for this query")
            return []

        channel_names = [ch.channel_type().value for ch in enabled]
        logger.info(
            "Executing {n} channel(s): {names}",
            n=len(enabled),
            names=channel_names,
        )

        # Launch all channels concurrently
        tasks = [self._safe_search(channel, context) for channel in enabled]
        results = await asyncio.gather(*tasks)

        # Log per-channel stats
        for result in results:
            level = "info" if result.chunks else "warning"
            getattr(logger, level)(
                "  {type}: {count} chunks in {ms:.0f}ms",
                type=result.channel_type.value,
                count=len(result.chunks),
                ms=result.latency_ms,
            )

        return list(results)

    async def _safe_search(self, channel: SearchChannel, context: SearchContext) -> ChannelResult:
        """
        Execute a single channel with timeout and error isolation.

        If the channel fails or times out, returns an empty ChannelResult
        instead of propagating the exception — other channels continue.
        """
        try:
            return await asyncio.wait_for(
                channel.search(context),
                timeout=self._channel_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Channel {type} timed out after {t}s",
                type=channel.channel_type().value,
                t=self._channel_timeout_s,
            )
            return ChannelResult.empty(channel.channel_type())
        except Exception as e:
            logger.error(
                "Channel {type} failed: {error}",
                type=channel.channel_type().value,
                error=str(e),
            )
            return ChannelResult.empty(channel.channel_type())
