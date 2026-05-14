"""
LangGraph state definitions for the podcast search + scrape pipeline.

Two separate graphs share these state types:
  1. SearchState  — used by the ReAct search graph (create_react_agent)
  2. ScrapeState  — used by the parallel URL-verification graph (Send fan-out)
  3. VerifyUrlInput — per-URL payload sent to each parallel verify node
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict, Annotated
import operator
from pydantic import BaseModel, Field


# ─── Pydantic models (shared with search_agent.py) ───────────────────────────


class ReturnItem(BaseModel):
    url: str = Field(..., description="The URL of the search result")
    title: str = Field(..., description="The title of the search result")
    description: str = Field(..., description="A brief description or summary of the content")
    source_name: str = Field(
        ...,
        description="The name/type of the source (e.g. 'wikipedia', 'google_news', 'general')",
    )
    tool_used: str = Field(
        ...,
        description="The tool used to find this result",
    )
    published_date: str = Field(
        default="",
        description="Publication date in ISO format; empty if unknown",
    )
    is_scrapping_required: bool = Field(
        default=True,
        description="True if the URL needs browser scraping; False if content is already complete",
    )


class SearchResults(BaseModel):
    items: List[ReturnItem] = Field(..., description="List of found source items")


# ─── Graph state TypedDicts ───────────────────────────────────────────────────


def _merge_results(a: List[Dict], b: List[Dict]) -> List[Dict]:
    """Merge two result lists, deduplicating by URL."""
    seen = {r.get("url") for r in a if r.get("url")}
    return a + [r for r in b if r.get("url") not in seen]


class SearchState(TypedDict):
    """
    State for the LangGraph ReAct search pipeline.

    'messages' is the standard LangGraph messages channel —
    the ReAct agent appends AIMessages (Thought / tool-call), ToolMessages
    (Observation), and a final AIMessage summary automatically.
    """

    query: str
    session_id: str
    # LangGraph messages channel; ReAct agent writes Thought/Action/Observation here
    messages: Annotated[list, operator.add]
    # Final structured results written by the format step
    search_results: List[Dict[str, Any]]


class ScrapeState(TypedDict):
    """
    State for the parallel URL-verification graph.

    'verified_results' uses operator.add so that results from N parallel
    verify_url nodes are automatically concatenated by LangGraph's reducer.
    'errors' uses the same strategy to accumulate per-node error messages.
    """

    query: str
    session_id: str
    # Input: output of the batch browser crawl (set once before fan-out)
    crawled_results: List[Dict[str, Any]]
    # Output: accumulated from all parallel verify_url nodes
    verified_results: Annotated[List[Dict[str, Any]], operator.add]
    # Errors accumulated from parallel nodes (non-fatal)
    errors: Annotated[List[str], operator.add]


class VerifyUrlInput(TypedDict):
    """
    Per-URL payload sent via Send() to each parallel verify_url node.
    This is NOT a full ScrapeState — LangGraph delivers it only to the
    target node; the node's return dict is then merged into ScrapeState.
    """

    item: Dict[str, Any]
    query: str
