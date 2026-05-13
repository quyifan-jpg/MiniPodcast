"""
Search agent tool — called by the main Agno orchestrator in celery_tasks.py.

Previously: created an Agno Agent with 10 tools and called .run().
            The LLM chose tools implicitly through OpenAI function-calling with
            no visible reasoning chain.

Now: delegates to graph.search_scrape_graph.run_search_pipeline() which runs a
     LangGraph ReAct agent (langgraph.prebuilt.create_react_agent).

     Explicit Thought / Action / Observation loop:
       ┌─ Thought  ─ LLM decides which tool to call
       ├─ Action   ─ tool is invoked (search_chunks, search_google_news, …)
       ├─ Observation ─ tool result fed back to LLM
       └─ … repeat until the agent is satisfied …
       └─ Format   ─ response_format step coerces output into SearchResults

     The full chain is printed to stdout, making the agent's reasoning
     fully observable without external tooling.

External API (used by celery_tasks.py):
  search_agent_run(agent, query) → str   ← unchanged

Re-exported models (kept here for backward-compat imports in other modules):
  ReturnItem, SearchResults
"""

from __future__ import annotations

from agno.agent import Agent

# Re-export Pydantic models from their new canonical location
from graph.state import ReturnItem, SearchResults  # noqa: F401


def search_agent_run(agent: Agent, query: str) -> str:
    """
    Agno tool: search for high-quality, diverse sources about the given topic.

    Internally runs a LangGraph ReAct loop with 10 project search tools.
    Results are saved into the shared session state so that subsequent tools
    (scrape_agent_run, podcast_script_agent_run, …) can access them.

    Args:
        agent: Agno Agent instance — provides agent.session_id.
        query: The search intent / topic (may contain typos; intent is inferred).

    Returns:
        Human-readable status string consumed by the main orchestrator as an
        Observation in its own (Agno) tool-calling loop.
    """
    print(f"\n[search_agent_run] query='{query}'")
    session_id = agent.session_id

    from services.internal_session_service import SessionService
    from graph.search_scrape_graph import run_search_pipeline

    session = SessionService.get_session(session_id)
    current_state = session["state"]

    # ── Run LangGraph ReAct search pipeline ──────────────────────────────────
    items = run_search_pipeline(query, session_id)

    # ── Persist results into session state ───────────────────────────────────
    current_state["stage"] = "search"
    current_state["search_results"] = items
    SessionService.save_session(session_id, current_state)

    count = len(items)
    if count:
        return (
            f"Found {count} sources about '{query}' and added them to "
            f"search_results. The sources are now visible in the UI for the "
            f"user to review and select from."
        )
    return (
        f"Search completed for '{query}' but no high-quality sources were found. "
        f"Consider asking the user for more details or trying a different query."
    )
