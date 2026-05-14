"""
LangChain-compatible tool wrappers for the LangGraph ReAct search agent.

Problem: the existing project tools all follow the Agno convention where the
first argument is an Agno Agent instance (used to read agent.session_id).
LangChain's @tool decorator expects plain functions with no injected agent.

Solution: _AgentShim captures session_id in a lightweight object that satisfies
the `.session_id` attribute contract, allowing the original tool functions to be
called without modification.  All wrappers are created inside make_search_tools()
so the shim is captured cleanly in each closure.
"""

from __future__ import annotations

import json
from langchain_core.tools import tool

# ── Agno-convention tools (first arg = agent) ────────────────────────────────
from tools.wikipedia_search import wikipedia_search
from tools.jikan_search import jikan_search
from tools.embedding_search import embedding_search
from tools.chunk_search import chunk_search
from tools.social_media_search import social_media_search, social_media_trending_search
from tools.search_articles import search_articles
from tools.web_search import run_browser_search

# ── Plain tool (no agent arg) ────────────────────────────────────────────────
from tools.google_news_discovery import google_news_discovery_run


class _AgentShim:
    """
    Minimal shim that satisfies `agent.session_id` for existing tools.
    No other Agent methods are used by the search/scrape tools.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


def make_search_tools(session_id: str) -> list:
    """
    Build and return a list of LangChain BaseTool instances ready for use
    with langgraph.prebuilt.create_react_agent.

    Each tool wraps an existing project function, adapting the Agno calling
    convention to plain LangChain tool semantics via _AgentShim.

    Tool ordering follows the SEARCH_AGENT_INSTRUCTIONS priority:
      chunk_search first → embedding_search → news/web tools → specialized → browser last.
    """
    shim = _AgentShim(session_id)

    # ── 1. Internal article database (passage-level) — ALWAYS call first ─────
    @tool
    def search_chunks(prompt: str) -> str:
        """Search the internal article database at the passage level and return
        the most relevant paragraphs. ALWAYS call this first for any topic before
        using external search tools."""
        return chunk_search(shim, prompt)

    # ── 2. Semantic embedding fallback ───────────────────────────────────────
    @tool
    def search_embeddings(prompt: str) -> str:
        """Search the internal article database using semantic embedding similarity.
        Use as a fallback if search_chunks returns no useful results."""
        return embedding_search(shim, prompt)

    # ── 3. Google News — preferred for news / current events ─────────────────
    @tool
    def search_google_news(keyword: str, max_results: int = 5) -> str:
        """Search Google News for recent news articles about the given keyword.
        Prefer this tool for news-related or current-events queries."""
        return google_news_discovery_run(keyword=keyword, max_results=max_results)

    # ── 4. DuckDuckGo — general web search ───────────────────────────────────
    @tool
    def search_duckduckgo(query: str) -> str:
        """Search the web using DuckDuckGo for general information about a topic."""
        from duckduckgo_search import DDGS

        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=8))
            # Normalise to the project's expected source-item shape
            normalized = [
                {
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "description": r.get("body", ""),
                    "source_name": "duckduckgo",
                    "tool_used": "duckduckgo",
                    "published_date": "",
                    "is_scrapping_required": True,
                }
                for r in raw
                if r.get("href")
            ]
            return f"is_scrapping_required: True, results: {json.dumps(normalized)}"
        except Exception as exc:
            return f"DuckDuckGo search error: {exc}"

    # ── 5. Wikipedia — encyclopedic background ────────────────────────────────
    @tool
    def search_wikipedia(query: str) -> str:
        """Search Wikipedia for encyclopedic background information on a topic."""
        return wikipedia_search(shim, query)

    # ── 6. Jikan — anime / manga topics only ─────────────────────────────────
    @tool
    def search_jikan(query: str) -> str:
        """Search Jikan (MyAnimeList API) for information about anime or manga.
        Use only when the topic is explicitly related to anime or manga."""
        return jikan_search(shim, query)

    # ── 7. Internal social media database ────────────────────────────────────
    @tool
    def search_social_media_posts(topic: str, limit: int = 10) -> str:
        """Search the internal social media database for posts that contain an
        exact keyword or phrase. The topic should be a specific keyword, not a
        full sentence."""
        return social_media_search(shim, topic, limit)

    # ── 8. Social media trending ──────────────────────────────────────────────
    @tool
    def get_social_media_trending(limit: int = 10) -> str:
        """Fetch currently trending topics from the internal social media database."""
        return social_media_trending_search(shim, limit)

    # ── 9. Internal articles database ────────────────────────────────────────
    @tool
    def search_articles_db(terms: str) -> str:
        """Search the internal articles database for content matching specific terms."""
        return search_articles(shim, terms)

    # ── 10. Browser agent — expensive, use as last resort ────────────────────
    @tool
    def search_with_browser(instruction: str) -> str:
        """Use a headless browser agent to navigate the web and collect detailed
        information. Provide step-by-step instructions on what to search for and
        what data to extract. Use this conservatively — it is significantly more
        expensive than other tools."""
        return run_browser_search(shim, instruction)

    return [
        search_chunks,  # always first per instructions
        search_embeddings,
        search_google_news,
        search_duckduckgo,
        search_wikipedia,
        search_jikan,
        search_social_media_posts,
        get_social_media_trending,
        search_articles_db,
        search_with_browser,  # always last (expensive)
    ]
