"""
Probe per-channel retrieval behavior on representative queries.

Run from the agent/ directory so module paths resolve:
    cd agent && python -m scripts.probe_channels

What you get:
    - Phase-1 logs from MultiChannelRetrievalEngine: each channel's
      raw hit count and latency (printed by engine.py via loguru).
    - A final-top-k breakdown by source_channel, so you can see which
      channels actually win after RRF fusion.

Tweak QUERIES to match the kinds of topics your users actually send.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

# Make `import rag.*` work when running as a script.
AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from rag.bridge import rag_search  # noqa: E402
from rag.models import RetrievedChunk  # noqa: E402


QUERIES: list[str] = [
    "GPT-4o multimodal capabilities",         # literal / acronym — keyword should help
    "AI startup latest funding rounds",       # time-sensitive — external/social bias
    "transformer attention mechanism",        # static knowledge — chunk/article vector
    "Sam Altman recent statements",           # named entity — mixed
    "Kubernetes operator pattern explained",  # technical jargon — keyword + vector
]

TOP_K = 10


def _summarize(chunks: list[RetrievedChunk]) -> dict[str, int]:
    return dict(Counter(ch.source_channel.value for ch in chunks))


def _preview(chunks: list[RetrievedChunk], n: int = 3) -> list[str]:
    out = []
    for ch in chunks[:n]:
        title = (ch.title or ch.content[:60]).strip().replace("\n", " ")
        out.append(f"  [{ch.source_channel.value}] {title[:80]}")
    return out


async def main() -> None:
    for q in QUERIES:
        print("\n" + "=" * 72)
        print(f"QUERY: {q}")
        print("=" * 72)
        chunks = await rag_search(q, top_k=TOP_K)
        print(f"\nfinal top-{TOP_K} by channel: {_summarize(chunks)}")
        print("top results:")
        for line in _preview(chunks):
            print(line)


if __name__ == "__main__":
    asyncio.run(main())
