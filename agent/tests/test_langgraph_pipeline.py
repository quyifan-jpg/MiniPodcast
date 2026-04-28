"""
Debug runner for the two new LangGraph pipelines.

Run from the agent/ directory:
    python -m tests.test_langgraph_pipeline

Or with a custom query:
    python -m tests.test_langgraph_pipeline --query "gene therapy" --test both

Tests:
  search  — ReAct search agent (Thought / Action / Observation chain)
  verify  — Parallel URL verification (Send fan-out)
  both    — run search first, feed results into verify (full pipeline)

No Celery, no FastAPI, no Redis required.
Only OPENAI_API_KEY in .env is needed.
"""

from __future__ import annotations

import sys
import os
import json
import argparse
import textwrap
import uuid
from pathlib import Path

# ── make sure agent/ is on sys.path ──────────────────────────────────────────
AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from dotenv import load_dotenv
load_dotenv(AGENT_DIR / ".env")

# ── colour helpers (no external deps) ────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"

def h1(text: str) -> None:
    bar = "═" * 70
    print(f"\n{BOLD}{CYAN}{bar}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{bar}{RESET}\n")

def h2(text: str) -> None:
    print(f"\n{BOLD}{YELLOW}── {text} ──{RESET}")

def ok(text: str) -> None:
    print(f"{GREEN}✓ {text}{RESET}")

def info(text: str) -> None:
    print(f"{DIM}  {text}{RESET}")

def err(text: str) -> None:
    print(f"{RED}✗ {text}{RESET}")

def section_result(label: str, data) -> None:
    """Pretty-print a result section."""
    print(f"\n{BOLD}{label}{RESET}")
    if isinstance(data, list):
        for i, item in enumerate(data, 1):
            if isinstance(item, dict):
                url   = item.get("url", "—")
                title = item.get("title", "—")
                tool  = item.get("tool_used", "—")
                has_text = bool(item.get("full_text"))
                verified = item.get("agent_verified", None)
                v_mark = (f"{GREEN}verified{RESET}" if verified
                          else f"{DIM}unverified{RESET}" if verified is not None
                          else "")
                print(f"  {i:2}. {BOLD}{title[:60]}{RESET}")
                print(f"      url:  {DIM}{url[:80]}{RESET}")
                print(f"      tool: {tool}  |  full_text: {'yes' if has_text else 'no'}  {v_mark}")
            else:
                print(f"  {i}. {item}")
    else:
        print(textwrap.indent(str(data), "  "))


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — ReAct Search Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def test_react_search(query: str, session_id: str) -> list:
    """
    Run the LangGraph ReAct search pipeline and pretty-print the full
    Thought / Action / Observation message chain.

    What to look for in the output:
      [AIMessage → Action]   — the LLM chose a tool (Thought + Action)
      [ToolMessage]          — the tool returned data (Observation)
      [AIMessage summary]    — the LLM's final synthesis (no tool calls)
      Structured output      — SearchResults Pydantic model from response_format
    """
    h1(f"TEST 1 — ReAct Search Pipeline")
    print(f"  Query      : {BOLD}{query}{RESET}")
    print(f"  Session ID : {DIM}{session_id}{RESET}")

    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    from langchain_core.messages import SystemMessage

    from graph.tools_registry import make_search_tools
    from graph.state import SearchResults
    from graph.search_scrape_graph import SEARCH_SYSTEM_PROMPT

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
    )
    tools = make_search_tools(session_id)

    ok(f"Agent built with {len(tools)} tools: {[t.name for t in tools]}")

    react_agent = create_react_agent(
        llm,
        tools,
        prompt=SEARCH_SYSTEM_PROMPT,
        response_format=SearchResults,
    )

    h2("Invoking ReAct agent — watch the Thought/Action/Observation chain")
    result = react_agent.invoke({
        "messages": [HumanMessage(content=f"Find diverse, high-quality sources about: {query}")]
    })

    # ── Pretty-print the full message chain ───────────────────────────────────
    h2("Full ReAct Message Chain")
    messages = result.get("messages", [])
    for i, msg in enumerate(messages):
        kind = type(msg).__name__
        if isinstance(msg, AIMessage) and msg.tool_calls:
            calls = [tc["name"] for tc in msg.tool_calls]
            args_preview = {tc["name"]: str(tc.get("args", {}))[:60]
                            for tc in msg.tool_calls}
            print(f"  {BOLD}[{i}] {CYAN}AIMessage → Action{RESET}")
            for name, args in args_preview.items():
                print(f"       tool: {GREEN}{name}{RESET}  args: {DIM}{args}{RESET}")
        elif isinstance(msg, ToolMessage):
            content_preview = str(msg.content)[:200].replace("\n", " ")
            print(f"  {BOLD}[{i}] {YELLOW}Observation({msg.name}){RESET}")
            print(f"       {DIM}{content_preview}…{RESET}")
        elif isinstance(msg, AIMessage) and msg.content:
            content_preview = str(msg.content)[:200].replace("\n", " ")
            print(f"  {BOLD}[{i}] {CYAN}AIMessage (Thought/Summary){RESET}")
            print(f"       {DIM}{content_preview}…{RESET}")
        elif isinstance(msg, HumanMessage):
            print(f"  {BOLD}[{i}] {DIM}HumanMessage: {str(msg.content)[:80]}{RESET}")
        else:
            print(f"  {BOLD}[{i}] {kind}{RESET}")

    # ── Structured output ─────────────────────────────────────────────────────
    h2("Structured Output (SearchResults)")
    structured: SearchResults | None = result.get("structured_response")
    if structured and structured.items:
        items = [item.model_dump() for item in structured.items]
        ok(f"{len(items)} sources returned")
        section_result("Sources", items)
        return items
    else:
        err("structured_response is empty — response_format may need a newer LangGraph")
        # fallback
        from graph.search_scrape_graph import _fallback_parse
        items = _fallback_parse(messages, query, llm)
        if items:
            ok(f"Fallback parser returned {len(items)} sources")
            section_result("Sources (fallback)", items)
        return items


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — Parallel Verify Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_CRAWLED_RESULTS = [
    {
        "url": "https://en.wikipedia.org/wiki/Gene_therapy",
        "title": "Gene therapy - Wikipedia",
        "description": "Gene therapy is a medical approach that treats or prevents disease.",
        "full_text": (
            "Gene therapy is a medical approach that treats or prevents disease by "
            "correcting the underlying genetic problem rather than just treating "
            "symptoms. The first gene therapy trial was done in 1990 by Dr. French "
            "Anderson. Modern techniques use CRISPR-Cas9 for precise editing. "
            "Applications include treatment of inherited diseases like sickle cell "
            "anaemia, cancer immunotherapy via CAR-T cells, and rare metabolic disorders. "
            "Challenges include delivery mechanisms, immune responses, and off-target effects."
        ),
        "source_name": "wikipedia",
        "tool_used": "search_wikipedia",
        "published_date": "",
        "is_scrapping_required": False,
    },
    {
        "url": "https://www.nature.com/articles/gene-therapy-2024",
        "title": "Recent advances in gene therapy",
        "description": "Nature article about gene therapy breakthroughs in 2024.",
        "full_text": (
            "Buy now! Click here! Special offer! Free trial!\n\n"
            "Recent Advances in Gene Therapy\n\n"
            "Researchers at Stanford have demonstrated a new lipid nanoparticle "
            "delivery system that improves efficiency by 40%. The study published "
            "in Nature Medicine shows promising results for liver-targeted therapies."
        ),
        "source_name": "general",
        "tool_used": "search_duckduckgo",
        "published_date": "2024-03-15",
        "is_scrapping_required": True,
    },
    {
        "url": "https://spam-site.example.com/buy-now",
        "title": "Buy cheap stuff online",
        "description": "Best deals on everything.",
        "full_text": "Buy now! Click here! Special offer limited time deal buy discount cheap sale!!!",
        "source_name": "general",
        "tool_used": "search_duckduckgo",
        "published_date": "",
        "is_scrapping_required": True,
    },
    {
        "url": "https://pubmed.ncbi.nlm.nih.gov/gene-therapy-crispr",
        "title": "CRISPR-based gene therapy clinical trials",
        "description": "Overview of ongoing clinical trials using CRISPR.",
        "full_text": (
            "Navigation | Home | About | Contact | Login\n\n"
            "CRISPR-based Gene Therapy: Clinical Trials Overview\n\n"
            "As of 2024, over 50 clinical trials are ongoing worldwide testing "
            "CRISPR-Cas9 gene editing for various diseases. Notable trials include "
            "sickle cell disease (CTX001), beta-thalassemia, and Leber congenital "
            "amaurosis. Early results show durable responses with acceptable safety profiles."
        ),
        "source_name": "pubmed",
        "tool_used": "search_duckduckgo",
        "published_date": "2024-01-10",
        "is_scrapping_required": True,
    },
]


def test_parallel_verify(crawled_results: list, query: str, session_id: str) -> list:
    """
    Run the parallel URL-verification graph and show per-URL results.

    What to look for in the output:
      Fan-out log   — N verify_url nodes dispatched simultaneously
      Per-node log  — each node prints its result immediately as it finishes
      Final summary — how many kept vs rejected, wall-clock time
    """
    h1("TEST 2 — Parallel URL Verification (LangGraph Send fan-out)")
    print(f"  Query      : {BOLD}{query}{RESET}")
    print(f"  URLs       : {len(crawled_results)}")
    print(f"  Session ID : {DIM}{session_id}{RESET}")

    import time
    from graph.search_scrape_graph import run_parallel_verify

    h2(f"Dispatching {len(crawled_results)} parallel verify_url nodes")
    info("(each node makes one LLM call — all run simultaneously via Send())")

    t0 = time.perf_counter()
    results = run_parallel_verify(crawled_results, query, session_id)
    elapsed = time.perf_counter() - t0

    h2("Results")
    kept     = [r for r in results if r.get("full_text")]
    rejected = [r for r in results if not r.get("full_text")]
    verified = [r for r in results if r.get("agent_verified")]

    ok(f"Completed in {elapsed:.2f}s  |  "
       f"{len(kept)} kept  |  {len(rejected)} rejected  |  {len(verified)} LLM-verified")

    section_result("Verified Sources", results)

    h2("Timing note")
    info(f"Sequential estimate : ~{len(crawled_results) * 2:.0f}s  (2s × {len(crawled_results)} URLs, sequential LLM calls)")
    info(f"Parallel actual     : {elapsed:.2f}s  (all {len(crawled_results)} LLM calls overlapped via Send)")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Debug runner for the LangGraph search + verify pipelines"
    )
    parser.add_argument(
        "--query", "-q",
        default="gene therapy CRISPR",
        help="Search topic (default: 'gene therapy CRISPR')",
    )
    parser.add_argument(
        "--test", "-t",
        choices=["search", "verify", "both"],
        default="both",
        help="Which test to run (default: both)",
    )
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        err("OPENAI_API_KEY not set — check agent/.env")
        sys.exit(1)

    session_id = str(uuid.uuid4())
    search_results = []

    if args.test in ("search", "both"):
        search_results = test_react_search(args.query, session_id)

    if args.test in ("verify", "both"):
        # Use real search results if available, otherwise use mock data
        crawled = search_results if search_results else MOCK_CRAWLED_RESULTS
        # Add dummy full_text to search results that don't have it yet
        for r in crawled:
            if not r.get("full_text"):
                r["full_text"] = r.get("description", "")
        test_parallel_verify(crawled, args.query, session_id)

    if args.test == "verify" and not search_results:
        h2("Note")
        info("Ran verify with MOCK data (4 synthetic URLs including 1 spam).")
        info("Run with --test both to verify real search results.")

    h1("Done")


if __name__ == "__main__":
    main()
