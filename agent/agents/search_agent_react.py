"""
ReAct version of search_agent using LangGraph's create_react_agent.

Differences from agno-based search_agent.py:
- LLM autonomously runs Thought → Action → Observation loops
- Tool selection emerges from observations, not from prompt rules
- Each step is traceable (printed) for debugging / interview demos

Drop-in compatible: exposes search_agent_react_run(agent, query) which the
main agno orchestrator can call exactly like the legacy search_agent_run.
"""

import os
import json
import traceback
from textwrap import dedent
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

# Reuse the existing tool implementations as-is.
# We pass `None` as the agent argument since they only use it for printing
# or session_id (browser_search is the one exception, handled via closure).
from tools.wikipedia_search import wikipedia_search as _wikipedia_impl
from tools.google_news_discovery import google_news_discovery_run as _google_news_impl
from tools.jikan_search import jikan_search as _jikan_impl
from tools.embedding_search import embedding_search as _embedding_impl
from tools.chunk_search import chunk_search as _chunk_impl
from tools.social_media_search import (
    social_media_search as _social_search_impl,
    social_media_trending_search as _social_trending_impl,
)
from tools.search_articles import search_articles as _search_articles_impl
from tools.web_search import run_browser_search as _browser_search_impl

load_dotenv()


# ─────────────────────────────────────────────────────────────────────
# Output schema (same as legacy search_agent.py)
# ─────────────────────────────────────────────────────────────────────
class ReturnItem(BaseModel):
    url: str
    title: str
    description: str
    source_name: str
    tool_used: str
    published_date: str = ""
    is_scrapping_required: bool = True


class SearchResults(BaseModel):
    items: List[ReturnItem]


# ─────────────────────────────────────────────────────────────────────
# Tool wrappers — convert (agent, query) signatures into LangChain @tool
# ─────────────────────────────────────────────────────────────────────
# A lightweight stand-in for agno's Agent so legacy tools that read
# `agent.session_id` don't crash. We populate it per-request.
class _AgentShim:
    def __init__(self, session_id: str = ""):
        self.session_id = session_id


# Module-level shim updated per call. ReAct tools are stateless from
# LangGraph's perspective, so we use a thread-local-ish module variable.
_current_shim = _AgentShim()


@tool
def chunk_search_tool(query: str) -> str:
    """Semantic passage-level search over the internal article database
    (RSS-crawled articles, indexed in Milvus HNSW).
    Returns the most relevant paragraphs grouped by article. ALWAYS try this
    first for any topic — it's the fastest, highest-quality source when
    relevant articles exist locally."""
    return _chunk_impl(_current_shim, query)


@tool
def embedding_search_tool(query: str) -> str:
    """Article-level semantic search over the internal database (similarity
    threshold 0.85). Use as a fallback when chunk_search_tool returns no
    results, or when you need entire articles instead of passages."""
    return _embedding_impl(_current_shim, query)


@tool
def google_news_tool(keyword: str, max_results: int = 5) -> str:
    """Search Google News for recent news articles by keyword. Best for
    current events, breaking news, and time-sensitive topics. Do NOT add
    dates to the keyword unless explicitly requested."""
    return _google_news_impl(keyword=keyword, max_results=max_results)


@tool
def wikipedia_tool(query: str) -> str:
    """Search Wikipedia for background knowledge, definitions, biographies,
    and historical context. Best for foundational/encyclopedic information,
    not for current events."""
    return _wikipedia_impl(_current_shim, query)


@tool
def jikan_tool(query: str) -> str:
    """Search MyAnimeList (via Jikan API) for anime information, reviews
    and recommendations. ONLY use for anime-related queries."""
    return _jikan_impl(_current_shim, query)


@tool
def social_media_search_tool(topic: str, limit: int = 10) -> str:
    """Search the internal social media database (X/Twitter etc.) for
    positive news posts about an exact topic keyword in the last 7 days.
    Use for trending opinions and viral content."""
    return _social_search_impl(_current_shim, topic, limit)


@tool
def social_media_trending_tool(limit: int = 10) -> str:
    """Get currently trending positive news posts from the social media
    database (no topic filter). Use when the user asks 'what's trending'
    or wants a broad pulse of current discussion."""
    return _social_trending_impl(_current_shim, limit)


@tool
def search_articles_tool(terms: str) -> str:
    """Plain SQL LIKE search over the internal articles database. Faster
    but less precise than chunk_search/embedding_search. Use only when
    semantic searches return nothing and you need a literal keyword match."""
    return _search_articles_impl(_current_shim, terms)


@tool
def browser_search_tool(instruction: str) -> str:
    """EXPENSIVE: Launch a real browser (Playwright) to perform a web
    search. Only use as a last resort when no other tool can find what's
    needed. Pass detailed step-by-step instructions for the browser agent."""
    return _browser_search_impl(_current_shim, instruction)


REACT_TOOLS = [
    chunk_search_tool,
    embedding_search_tool,
    google_news_tool,
    wikipedia_tool,
    jikan_tool,
    social_media_search_tool,
    social_media_trending_tool,
    search_articles_tool,
    browser_search_tool,
]


# ─────────────────────────────────────────────────────────────────────
# ReAct system prompt
# ─────────────────────────────────────────────────────────────────────
REACT_SYSTEM_PROMPT = dedent("""
You are a research agent that finds high-quality, diverse sources for a
podcast on a given topic.

# Strategy
1. ALWAYS start with chunk_search_tool — the internal article database is
   the cheapest, fastest source.
2. Inspect what came back. If the results are sparse, outdated, or
   irrelevant, reason about WHY and pick a complementary tool:
   - News-related / time-sensitive       → google_news_tool
   - Background / definitions / history  → wikipedia_tool
   - Anime queries                       → jikan_tool
   - Trending opinions, viral takes      → social_media_*_tool
   - Literal keyword match needed        → search_articles_tool
   - Nothing else worked                 → browser_search_tool (expensive!)
3. STOP calling tools as soon as you have 3+ usable sources from ANY tool.
   Do NOT keep searching to be thorough — return what you have.
4. Hard limit: at most 5 tool calls total. After 5 calls you MUST output JSON
   even if you have only a few results.
5. Avoid duplicates. Prefer reputable sources. Drop low-quality results.
6. If a tool errors out (403, "not available", "no results"), try ONE
   alternative — do not retry the same tool with different keywords more
   than once.

# Output format
After your final tool call, return ONLY a JSON object matching this schema
(no markdown fences, no commentary):

{
  "items": [
    {
      "url": "...",
      "title": "...",
      "description": "short summary",
      "source_name": "wikipedia | google_news | duckduckgo | social | chunk | embedding | jikan | browser | general",
      "tool_used": "<name of the tool that produced this item>",
      "published_date": "ISO date or empty string",
      "is_scrapping_required": true
    }
  ]
}

Set is_scrapping_required=true unless the tool already returned full content
(chunk_search, embedding_search, jikan, social_media all return ready content).
""").strip()


# ─────────────────────────────────────────────────────────────────────
# Build the ReAct agent (lazy singleton)
# ─────────────────────────────────────────────────────────────────────
_react_agent = None


def _get_react_agent():
    global _react_agent
    if _react_agent is None:
        llm = ChatOpenAI(
            model="gpt-4o-mini",
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
        )
        _react_agent = create_react_agent(
            model=llm,
            tools=REACT_TOOLS,
            prompt=REACT_SYSTEM_PROMPT,
        )
    return _react_agent


# ─────────────────────────────────────────────────────────────────────
# Result parsing
# ─────────────────────────────────────────────────────────────────────
def _extract_json(text: str) -> Optional[dict]:
    """Extract first JSON object from a string (tolerates ```json fences)."""
    if not text:
        return None
    # Strip common markdown fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: find first {...} block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _trace_react_run(messages):
    """Print the full ReAct trace for observability."""
    print("\n" + "=" * 60)
    print("[ReAct Trace]")
    print("=" * 60)
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"  [{i}] 💭 Action: {tc['name']}({tc['args']})")
            elif msg.content:
                preview = msg.content[:200].replace("\n", " ")
                print(f"  [{i}] 🎯 Final: {preview}...")
        elif isinstance(msg, ToolMessage):
            preview = str(msg.content)[:150].replace("\n", " ")
            print(f"  [{i}] 👁  Observation ({msg.name}): {preview}...")
    print("=" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────
# Public entry point — drop-in replacement for search_agent_run
# ─────────────────────────────────────────────────────────────────────
def search_agent_react_run(agent, query: str) -> str:
    """
    ReAct-based search agent. Drop-in replacement for agents.search_agent.search_agent_run.

    Args:
        agent: The orchestrator agno Agent instance (used only for session_id)
        query: The search query

    Returns:
        Status string. Side effect: writes results into session_state["search_results"]
    """
    print(f"\n[ReAct Search] 收到查询: {query}")

    session_id = getattr(agent, "session_id", "") or ""
    _current_shim.session_id = session_id

    from services.internal_session_service import SessionService

    session = SessionService.get_session(session_id)
    current_state = session["state"]

    try:
        react_agent = _get_react_agent()
        result = react_agent.invoke(
            {"messages": [HumanMessage(content=query)]},
            config={"recursion_limit": 25},  # ~12 thought-action loops
        )

        messages = result.get("messages", [])
        _trace_react_run(messages)

        # The last AIMessage without tool_calls is the final answer
        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        parsed = _extract_json(final_text)
        if not parsed or "items" not in parsed:
            print(f"[ReAct Search] ✗ 无法解析最终输出为 JSON: {final_text[:300]}")
            return f"Search completed but result parsing failed. Raw output: {final_text[:500]}"

        # Validate against pydantic schema (drops malformed items)
        try:
            validated = SearchResults(**parsed)
            items = [item.model_dump() for item in validated.items]
        except Exception as e:
            print(f"[ReAct Search] ✗ Pydantic 校验失败: {e}")
            items = parsed.get("items", [])

        current_state["stage"] = "search"
        current_state["search_results"] = items
        SessionService.save_session(session_id, current_state)

        print(f"[ReAct Search] ✓ 找到 {len(items)} 个来源")
        return f"Found {len(items)} sources about '{query}' and added to search_results"

    except Exception as e:
        print(f"[ReAct Search] ✗ 异常: {e}")
        traceback.print_exc()
        return f"Search failed: {str(e)}"


# ─────────────────────────────────────────────────────────────────────
# CLI test
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    class _FakeAgent:
        session_id = "test_react_session"

    print(search_agent_react_run(_FakeAgent(), "AI breakthroughs in healthcare 2025"))
