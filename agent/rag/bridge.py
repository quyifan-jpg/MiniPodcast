"""
Bridge between the multi-channel retrieval engine and the existing
search_agent / LangGraph pipeline.

This module provides:
  1. rag_search() — async function that replaces run_search_pipeline()
  2. rag_search_tool() — LangChain tool wrapper for use in ReAct agents
  3. rag_search_agent_run() — Agno tool wrapper (drop-in for search_agent_run)

Migration path:
  - Phase 1: Use rag_search_agent_run() alongside existing search_agent_run()
  - Phase 2: Replace search_agent_run() with rag_search_agent_run() in celery_tasks
  - Phase 3: Remove the old LangGraph ReAct search pipeline
"""

from __future__ import annotations

import asyncio

from loguru import logger

from rag.models import RetrievedChunk, SearchContext


async def rag_search(
    query: str,
    top_k: int = 10,
    rewrite_query: bool = False,
) -> list[RetrievedChunk]:
    """
    Execute the multi-channel retrieval engine.

    This is the pure async function — use it directly in async contexts.

    Args:
        query: Search query string.
        top_k: Maximum number of final results.
        rewrite_query: If True, use LLM to expand/rewrite the query
                       for better vector recall.

    Returns:
        List of RetrievedChunk objects, ranked and deduplicated.
    """
    from rag.factory import get_retrieval_engine

    engine = get_retrieval_engine()

    # Optional: LLM query rewriting for better vector recall
    rewritten = ""
    if rewrite_query:
        rewritten = await _rewrite_query(query)

    context = SearchContext(
        original_query=query,
        rewritten_query=rewritten,
        top_k=top_k,
    )

    return await engine.retrieve(context)


def rag_search_sync(
    query: str,
    top_k: int = 10,
    rewrite_query: bool = False,
) -> list[RetrievedChunk]:
    """
    Synchronous wrapper for rag_search().

    For use in Celery tasks and other sync contexts where asyncio.run()
    is needed.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context — create a new thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, rag_search(query, top_k, rewrite_query))
            return future.result()
    else:
        return asyncio.run(rag_search(query, top_k, rewrite_query))


def chunks_to_search_results(chunks: list[RetrievedChunk]) -> list[dict]:
    """
    Convert RetrievedChunk list to the dict format expected by the
    existing session state / scrape pipeline.

    Maps to the ReturnItem schema used throughout the codebase:
    {url, title, description, source_name, tool_used, published_date,
     is_scrapping_required}
    """
    results = []
    for chunk in chunks:
        is_scrapping = chunk.metadata.get("is_scrapping_required", False)
        results.append(
            {
                "id": chunk.id,
                "url": chunk.url,
                "title": chunk.title,
                "description": chunk.content[:300] if chunk.content else "",
                "full_text": chunk.content if not is_scrapping else "",
                "source_name": chunk.source_channel.value,
                "tool_used": f"rag_{chunk.source_channel.value}",
                "published_date": chunk.metadata.get("published_date", ""),
                "is_scrapping_required": is_scrapping,
                "similarity": round(chunk.score, 3),
                "categories": [chunk.source_channel.value],
            }
        )
    return results


def rag_search_agent_run(agent, query: str) -> str:
    """
    Drop-in replacement for search_agent_run() in agents/search_agent.py.

    Same interface: takes an Agno agent + query, returns a status string.
    Internally uses the multi-channel retrieval engine instead of the
    LangGraph ReAct search pipeline.

    Args:
        agent: Agno Agent instance — provides agent.session_id.
        query: Search query string.

    Returns:
        Human-readable status string for the Agno orchestrator.
    """
    logger.info("[RAG search] query='{q}' session={s}", q=query, s=agent.session_id)
    session_id = agent.session_id

    from services.internal_session_service import SessionService

    session = SessionService.get_session(session_id)
    current_state = session["state"]

    # Run retrieval
    chunks = rag_search_sync(query, top_k=10, rewrite_query=True)

    # Convert to existing format
    items = chunks_to_search_results(chunks)

    # Persist to session state (same as original search_agent_run)
    current_state["stage"] = "search"
    current_state["search_results"] = items
    SessionService.save_session(session_id, current_state)

    # Build status message
    count = len(items)
    channel_summary = _channel_summary(chunks)

    if count:
        return (
            f"Found {count} sources about '{query}' via multi-channel retrieval "
            f"({channel_summary}). Results are now visible in the UI for the "
            f"user to review and select from."
        )
    return (
        f"Multi-channel search completed for '{query}' but no high-quality "
        f"sources were found across any channel. Consider asking the user "
        f"for more details or trying a different query."
    )


# ── Private helpers ───────────────────────────────────────────────────


def _channel_summary(chunks: list[RetrievedChunk]) -> str:
    """E.g., 'chunk_vector: 5, keyword: 3, external: 2'"""
    counts: dict[str, int] = {}
    for c in chunks:
        name = c.source_channel.value
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in counts.items())


async def _rewrite_query(query: str) -> str:
    """
    Use LLM to expand the query for better vector retrieval.

    Example:
      Input:  "K8s HPA"
      Output: "Kubernetes Horizontal Pod Autoscaler configuration scaling"
    """
    try:
        from services.model_router import router

        llm = router.get_chat_model()
        response = await asyncio.to_thread(
            llm.invoke,
            [
                {
                    "role": "system",
                    "content": (
                        "Expand the user's search query into a richer version "
                        "that includes synonyms, related terms, and full forms "
                        "of abbreviations. Return ONLY the expanded query, "
                        "no explanation. Keep it under 30 words."
                    ),
                },
                {"role": "user", "content": query},
            ],
        )
        rewritten = response.content.strip()
        logger.info(
            "Query rewrite: '{original}' → '{rewritten}'",
            original=query,
            rewritten=rewritten,
        )
        return rewritten
    except Exception as e:
        logger.warning("Query rewrite failed, using original: {e}", e=e)
        return query
