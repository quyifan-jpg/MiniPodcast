"""
Two LangGraph pipelines that replace the previous Agno-based implementations:

  1. run_search_pipeline()
     ─────────────────────
     Uses langgraph.prebuilt.create_react_agent to run a genuine ReAct loop:

       Thought  → decide which search tool to call
       Action   → call the tool
       Observation → receive tool result
       … repeat until enough sources are collected …
       Format   → extract structured SearchResults (response_format)

     The full Thought/Action/Observation chain is logged so you can inspect
     the agent's reasoning at each step.

  2. run_parallel_verify()
     ──────────────────────
     Uses a StateGraph with LangGraph's Send() API to fan-out URL verification
     across all crawled results in parallel:

       START
         ↓  _route_to_verify  (conditional edge → Send per URL)
       verify_url × N   (each runs an LLM quality-check in parallel)
         ↓  operator.add reducer accumulates verified_results
       END

     Previously, verify_content_with_agent() looped sequentially over every
     URL and ran a blocking LLM call for each.  With Send(), all N LLM calls
     are dispatched simultaneously and results are merged automatically.

Integration: both functions are called from the existing Agno tool wrappers in
agents/search_agent.py and agents/scrape_agent.py — celery_tasks.py is
unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import create_react_agent
from langgraph.types import Send
from pydantic import BaseModel, Field

from graph.state import ScrapeState, SearchResults, VerifyUrlInput
from graph.tools_registry import make_search_tools


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 — ReAct search graph
# ═══════════════════════════════════════════════════════════════════════════════

SEARCH_SYSTEM_PROMPT = """\
You are a research assistant that finds high-quality, diverse sources for podcast episodes.

Your workflow:
1. ALWAYS call search_chunks first — it queries the internal article database.
2. Fall back to search_embeddings if search_chunks returns nothing.
3. For news/current-events topics use search_google_news.
4. Use search_duckduckgo for general web searches.
5. Use search_wikipedia for encyclopedic background.
6. Use search_jikan ONLY for anime / manga topics.
7. Use search_with_browser sparingly — only when all other tools are insufficient.

Rules:
- Return 5–10 sources total; favour quality and diversity over quantity.
- Never add dates to a search query unless the user explicitly asked for them.
- Deduplicate: never include the same URL twice.
- User queries may contain spelling mistakes — infer intent and proceed.
- If a tool returns low-quality or irrelevant content, discard it.

When you are satisfied with the sources you have found, stop calling tools.
The framework will then format your findings into structured output automatically.
"""


def _log_react_chain(messages: list, label: str = "ReAct Search") -> None:
    """Print the full Thought / Action / Observation chain for observability."""
    print(f"\n[{label}] ── message chain ({len(messages)} messages) ──")
    for i, msg in enumerate(messages):
        kind = type(msg).__name__
        if isinstance(msg, AIMessage) and msg.tool_calls:
            calls = [tc["name"] for tc in msg.tool_calls]
            print(f"  [{i}] {kind} → Action: {calls}")
        elif isinstance(msg, ToolMessage):
            preview = str(msg.content)[:120].replace("\n", " ")
            print(f"  [{i}] Observation({msg.name}): {preview}…")
        elif isinstance(msg, AIMessage) and msg.content:
            preview = str(msg.content)[:120].replace("\n", " ")
            print(f"  [{i}] {kind} (Thought/Summary): {preview}…")
        else:
            print(f"  [{i}] {kind}")
    print(f"[{label}] ── end ──\n")


def run_search_pipeline(query: str, session_id: str) -> List[Dict[str, Any]]:
    """
    Run a LangGraph ReAct search agent and return a list of source dicts.

    The agent uses the full set of project search tools wrapped as LangChain
    tools.  Each tool call is visible in the logged message chain as an explicit
    Action → Observation pair.

    After the ReAct loop, create_react_agent's response_format parameter adds a
    final formatting step that coerces the conversation into a structured
    SearchResults Pydantic model.  If that step is unavailable (older LangGraph),
    _fallback_parse() handles extraction via a second LLM call.

    Args:
        query:      The user's topic / search intent.
        session_id: Used by tools that need session-scoped state (browser tool).

    Returns:
        List of dicts, each matching the ReturnItem schema:
        {url, title, description, source_name, tool_used, published_date,
         is_scrapping_required}
    """
    from services.model_router import router
    llm = router.get_chat_model()
    tools = make_search_tools(session_id)

    react_agent = create_react_agent(
        llm,
        tools,
        prompt=SEARCH_SYSTEM_PROMPT,
        response_format=SearchResults,
    )

    print(f"\n[ReAct Search] Starting pipeline for query: '{query}'")
    result = react_agent.invoke({
        "messages": [
            HumanMessage(content=f"Find diverse, high-quality sources about: {query}")
        ]
    })

    # Log the full Thought/Action/Observation chain
    _log_react_chain(result.get("messages", []))

    # Primary path: structured_response populated by response_format step
    structured: SearchResults | None = result.get("structured_response")
    if structured and structured.items:
        items = [item.model_dump() for item in structured.items]
        print(f"[ReAct Search] Structured output: {len(items)} sources found")
        return items

    # Fallback path: older LangGraph or empty structured_response
    print("[ReAct Search] structured_response unavailable — using fallback parser")
    return _fallback_parse(result.get("messages", []), query, llm)


def _fallback_parse(
    messages: list,
    query: str,
    llm: ChatOpenAI,
) -> List[Dict[str, Any]]:
    """
    Extract SearchResults from the ReAct message chain using a second LLM call
    with structured output.  Used when response_format is not available.
    """
    # Collect all tool observations as the evidence base
    observations: List[str] = [
        f"Tool '{msg.name}' returned:\n{str(msg.content)[:600]}"
        for msg in messages
        if isinstance(msg, ToolMessage) and msg.content
    ]
    if not observations:
        return []

    formatter = llm.with_structured_output(SearchResults)
    try:
        formatted: SearchResults = formatter.invoke([
            SystemMessage(content=(
                "Based on the tool results below, extract a list of distinct, "
                "high-quality source items. Each item must have: url, title, "
                "description, source_name, tool_used, published_date, "
                "is_scrapping_required."
            )),
            HumanMessage(content=(
                f"Search query: {query}\n\n"
                + "\n\n".join(observations)
            )),
        ])
        items = [item.model_dump() for item in formatted.items] if formatted else []
        print(f"[ReAct Search] Fallback parser: {len(items)} sources extracted")
        return items
    except Exception as exc:
        print(f"[ReAct Search] Fallback parser failed: {exc}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 — Parallel URL verification graph
# ═══════════════════════════════════════════════════════════════════════════════

VERIFY_SYSTEM_PROMPT = """\
You are a content quality-verification assistant for a podcast production pipeline.

Given a URL's scraped text and the original search query you must:
1. Check that the content is relevant to the query.
2. Strip navigation menus, ads, cookie notices, and formatting artefacts.
3. Assess quality: reject content that is too short (< 100 words), spammy, or off-topic.
4. Return the cleaned text at a sensible length (keep the substance, trim padding).
5. Extract the publication date if present.

Return an empty full_text string for pages that are irrelevant or too low quality.
"""


class _VerifiedContent(BaseModel):
    """Structured output for the per-URL LLM verification step."""
    full_text: str = Field(
        description="Cleaned, relevant text content. Empty string if irrelevant or low quality."
    )
    published_date: str = Field(
        default="",
        description="Publication date in ISO format (YYYY-MM-DD). Empty if not found.",
    )


def _verify_single_url(state: VerifyUrlInput) -> Dict[str, Any]:
    """
    Verify and clean a single URL's scraped content via an LLM call.

    This function is the node body for the 'verify_url' node.  LangGraph
    invokes it N times in parallel (one per Send() payload).  Its return dict
    is merged into ScrapeState via the operator.add reducer on verified_results.

    Args:
        state: VerifyUrlInput — contains 'item' (the crawled result dict) and
               'query' (the original search query for relevance checking).

    Returns:
        dict with keys:
          verified_results: List[Dict]  — single-element list (for reducer)
          errors:           List[str]   — empty on success, error message on fail
    """
    item: Dict[str, Any] = state["item"]
    query: str = state["query"]
    url = item.get("url", "")

    # Skip LLM call if there is no content to verify
    if not item.get("full_text"):
        return {"verified_results": [item], "errors": []}

    try:
        from services.model_router import router
        llm = router.get_chat_model()
        verifier = llm.with_structured_output(_VerifiedContent)

        result: _VerifiedContent = verifier.invoke([
            SystemMessage(content=VERIFY_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Query: {query}\n"
                f"URL: {url}\n\n"
                f"Content to verify:\n{item['full_text'][:3000]}"
            )),
        ])

        verified_item = {
            **item,
            "full_text": result.full_text,
            "published_date": result.published_date or item.get("published_date", ""),
            "agent_verified": True,
        }
        kept_chars = len(result.full_text)
        status = "kept" if result.full_text else "rejected (low quality)"
        print(f"  [verify_url] {status} — {url[:70]} ({kept_chars} chars)")
        return {"verified_results": [verified_item], "errors": []}

    except Exception as exc:
        # Non-fatal: return the original item unmodified
        print(f"  [verify_url] ERROR — {url[:70]}: {exc}")
        return {
            "verified_results": [{**item, "agent_verified": False}],
            "errors": [f"Verify failed for {url}: {exc}"],
        }


def _route_to_verify(state: ScrapeState) -> List[Send]:
    """
    Conditional edge function: fans out one Send per crawled result.

    LangGraph calls this after the START node.  Each Send creates an
    independent invocation of 'verify_url' with its own VerifyUrlInput
    payload.  Because verified_results uses operator.add, all results are
    automatically concatenated when the parallel nodes complete.
    """
    crawled = state.get("crawled_results", [])
    query = state["query"]
    print(f"\n[Parallel Verify] Fanning out to {len(crawled)} parallel verify nodes")
    return [
        Send("verify_url", {"item": item, "query": query})
        for item in crawled
    ]


# Module-level compiled graph (built once, reused across Celery tasks)
_parallel_verify_graph = None


def _build_parallel_verify_graph():
    """Build and compile the parallel URL-verification StateGraph."""
    graph = StateGraph(ScrapeState)

    # Single worker node — LangGraph will instantiate it N times in parallel
    graph.add_node("verify_url", _verify_single_url)

    # Fan-out: START → Send("verify_url", payload) × N
    graph.add_conditional_edges(START, _route_to_verify, ["verify_url"])

    # Fan-in: each verify_url → END (reducer merges verified_results)
    graph.add_edge("verify_url", END)

    return graph.compile()


def run_parallel_verify(
    crawled_results: List[Dict[str, Any]],
    query: str,
    session_id: str,
) -> List[Dict[str, Any]]:
    """
    Run LLM-based quality verification on all crawled URLs in parallel.

    Instead of the previous sequential loop (one LLM call → next URL → repeat),
    this dispatches all N verification tasks simultaneously via LangGraph's
    Send API.  The operator.add reducer accumulates results as nodes complete.

    Args:
        crawled_results: Output of crawl_urls_batch() — list of source dicts
                         with full_text already populated by the browser crawler.
        query:           Original search query for relevance checking.
        session_id:      Passed through to state for traceability.

    Returns:
        List of verified/cleaned source dicts.  Items where the LLM judged the
        content irrelevant will have full_text set to "".
    """
    global _parallel_verify_graph
    if _parallel_verify_graph is None:
        _parallel_verify_graph = _build_parallel_verify_graph()

    if not crawled_results:
        return []

    initial_state: ScrapeState = {
        "query": query,
        "session_id": session_id,
        "crawled_results": crawled_results,
        "verified_results": [],
        "errors": [],
    }

    print(f"\n[Parallel Verify] Starting for {len(crawled_results)} URLs, query='{query}'")
    final_state = _parallel_verify_graph.invoke(initial_state)

    errors = final_state.get("errors", [])
    if errors:
        print(f"[Parallel Verify] Non-fatal errors ({len(errors)}): {errors}")

    results = final_state.get("verified_results", [])
    print(f"[Parallel Verify] Complete: {len(results)} results returned\n")
    return results
