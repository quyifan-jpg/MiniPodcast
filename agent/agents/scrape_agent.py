"""
Scrape agent tool — called by the main Agno orchestrator in celery_tasks.py.

Previously: verify_content_with_agent() looped over each URL one-by-one,
            making a blocking LLM call for each (sequential verification).
            This was O(n) LLM round-trips before the pipeline could continue.

Now: after the batch browser crawl (crawl_urls_batch — unchanged), quality
     verification is delegated to graph.search_scrape_graph.run_parallel_verify()
     which uses a LangGraph StateGraph with the Send() API to fan-out all
     verification LLM calls simultaneously:

       START
         │  _route_to_verify (one Send per crawled URL)
         ├──► verify_url ──┐
         ├──► verify_url ──┤  (all N run in parallel)
         └──► verify_url ──┘
                           │  operator.add reducer merges results
                          END

     Wall-clock time for the verification step drops from O(n × latency) to
     O(1 × latency) — all LLM calls overlap.

External API (used by celery_tasks.py):
  scrape_agent_run(agent, query) → str   ← unchanged
"""

from __future__ import annotations

from agno.agent import Agent
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from tools.browser_crawler import create_browser_crawler
from textwrap import dedent

load_dotenv()


# ── Pydantic model kept for any downstream code that imports it ───────────────

class ScrapedContent(BaseModel):
    url: str = Field(..., description="The URL of the search result")
    description: str = Field(description="The description of the search result")
    full_text: str = Field(
        ...,
        description="The full text of the given source URL; empty if not available",
    )
    published_date: str = Field(
        ...,
        description="Publication date in ISO format; empty if not available",
    )


# ── Batch browser crawl (unchanged from original) ────────────────────────────

def crawl_urls_batch(search_results):
    """
    Browser-crawl all URLs in search_results that require scraping.

    Groups by unique URL to avoid duplicate network requests, then dispatches
    a single browser_crawler.scrape_urls() call for the whole batch.  The
    browser crawler already handles concurrency internally.

    Returns:
        (updated_results, successful_scrapes, failed_scrapes)
    """
    url_to_search_results = {}
    unique_urls = []
    for search_result in search_results:
        if not search_result.get("url", False):
            continue
        if not search_result.get("is_scrapping_required", True):
            continue
        if not search_result.get("original_url"):
            search_result["original_url"] = search_result["url"]
        url = search_result["url"]
        if url not in url_to_search_results:
            url_to_search_results[url] = []
            unique_urls.append(url)
        url_to_search_results[url].append(search_result)

    browser_crawler = create_browser_crawler()
    scraped_results = browser_crawler.scrape_urls(unique_urls)
    url_to_scraped = {result["original_url"]: result for result in scraped_results}

    updated_search_results = []
    successful_scrapes = 0
    failed_scrapes = 0
    for search_result in search_results:
        original_url = search_result["url"]
        scraped = url_to_scraped.get(original_url, {})
        updated_result = search_result.copy()
        updated_result["original_url"] = original_url
        if scraped.get("success", False):
            updated_result["url"] = scraped.get("final_url", original_url)
            updated_result["full_text"] = scraped.get("full_text", "")
            updated_result["published_date"] = scraped.get("published_date", "")
            successful_scrapes += 1
        else:
            updated_result["url"] = original_url
            updated_result["full_text"] = search_result.get("description", "")
            updated_result["published_date"] = ""
            failed_scrapes += 1
        updated_search_results.append(updated_result)

    return updated_search_results, successful_scrapes, failed_scrapes


# ── Scrape agent tool ─────────────────────────────────────────────────────────

def scrape_agent_run(agent: Agent, query: str) -> str:
    """
    Agno tool: fetch full content for each search result and verify quality.

    Step 1 — Batch browser crawl (crawl_urls_batch)
      All unique URLs are scraped in a single batch by the browser crawler.
      This step is I/O-bound and already grouped; no change here.

    Step 2 — Parallel LLM verification (run_parallel_verify)
      Previously: sequential loop → 1 LLM call at a time.
      Now: LangGraph Send() API → all N LLM calls dispatched simultaneously.
      Each verify_url node checks relevance, strips noise, and cleans text.
      Results are merged automatically via the operator.add reducer.

    Args:
        agent: Agno Agent instance — provides agent.session_id.
        query: The original search query, used for relevance checking.

    Returns:
        Human-readable status string (consumed as an Observation by the main
        orchestrator).
    """
    print(f"\n[scrape_agent_run] query='{query}'")
    session_id = agent.session_id

    from services.internal_session_service import SessionService
    from graph.search_scrape_graph import run_parallel_verify

    session = SessionService.get_session(session_id)
    current_state = session["state"]

    # ── Step 1: batch browser crawl ──────────────────────────────────────────
    print(f"[scrape_agent_run] Step 1 — batch browser crawl")
    updated_results, successful, failed = crawl_urls_batch(current_state["search_results"])
    print(f"[scrape_agent_run] Crawl complete: {successful} ok, {failed} failed")

    # ── Step 2: parallel LLM verification (LangGraph Send fan-out) ───────────
    print(f"[scrape_agent_run] Step 2 — parallel LangGraph verification")
    verified_results = run_parallel_verify(updated_results, query, session_id)

    # ── Persist into session state ────────────────────────────────────────────
    current_state["search_results"] = verified_results
    SessionService.save_session(session_id, current_state)

    count = len(verified_results)
    kept = sum(1 for r in verified_results if r.get("full_text"))
    return (
        f"Scraped and verified {count} sources for '{query}'. "
        f"{kept} sources have full content; {count - kept} have description-only fallback. "
        f"Browser crawl: {successful} succeeded, {failed} failed."
    )
