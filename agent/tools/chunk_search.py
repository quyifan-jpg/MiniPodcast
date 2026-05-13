"""
Chunk-level semantic search backed by Milvus.

Queries the `chunk_vectors` Milvus collection for the most relevant
passages, then groups and re-ranks by article.
"""

import json

from agno.agent import Agent
from openai import OpenAI

from db.config import get_tracking_db_path
from db.connection import execute_query
from db.milvus import get_milvus
from utils.load_api_keys import load_api_key

EMBEDDING_MODEL      = "text-embedding-3-small"
TOP_K                = 30    # chunks to retrieve from Milvus
MAX_CHUNKS_PER_ARTICLE = 3   # keep at most N chunks per article
MIN_SIMILARITY       = 0.25  # IP score threshold (chunks have lower avg similarity than full articles)
FINAL_TOP_K          = 8     # articles to return


def _generate_query_embedding(query: str) -> list[float] | None:
    api_key = load_api_key("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        client = OpenAI(api_key=api_key)
        resp = client.embeddings.create(input=query, model=EMBEDDING_MODEL)
        return resp.data[0].embedding
    except Exception as e:
        print(f"chunk_search: embedding error: {e}")
        return None


def _get_article_metadata(db_path: str, article_ids: list[int]) -> dict[int, dict]:
    """Fetch published_date and source_id from MySQL (not stored in Milvus)."""
    if not article_ids:
        return {}
    placeholders = ",".join(["%s"] * len(article_ids))
    rows = execute_query(
        db_path,
        f"SELECT id, published_date, source_id FROM crawled_articles WHERE id IN ({placeholders})",
        article_ids,
        fetch=True,
    )
    return {row["id"]: row for row in rows} if rows else {}


def _group_and_rerank(hits: list[dict], meta_map: dict[int, dict]) -> list[dict]:
    """Group top chunks by article, deduplicate, assemble passage context."""
    by_article: dict[int, list] = {}
    for hit in hits:
        aid = hit["article_id"]
        if aid not in by_article:
            by_article[aid] = []
        if len(by_article[aid]) < MAX_CHUNKS_PER_ARTICLE:
            by_article[aid].append(hit)

    results = []
    for aid, chunk_hits in by_article.items():
        # Sort by chunk_index for readability
        chunk_hits.sort(key=lambda h: h["chunk_index"])
        best_score = max(h["score"] for h in chunk_hits)
        passages   = "\n\n---\n\n".join(h["chunk_text"] for h in chunk_hits)
        meta       = meta_map.get(aid, {})

        results.append({
            "id":             aid,
            "title":          chunk_hits[0].get("title", "Untitled"),
            "url":            chunk_hits[0].get("url", ""),
            "published_date": str(meta.get("published_date", "")),
            "source_id":      str(meta.get("source_id", "")),
            "full_text":      passages,
            "description":    passages,
            "similarity":     round(best_score, 3),
            "chunks_used":    len(chunk_hits),
            "is_scrapping_required": False,
            "categories":     ["chunk-semantic"],
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:FINAL_TOP_K]


def chunk_search(agent: Agent, prompt: str) -> str:
    """
    Semantic chunk-level search over internal articles using Milvus.

    Retrieves the most relevant *passages* (not whole articles) from the
    knowledge base using vector similarity, then returns them grouped by
    article so the script agent can generate a richer, more grounded podcast.

    Args:
        agent:  Agno agent instance
        prompt: Search query describing the podcast topic

    Returns:
        JSON string with top articles and their most relevant passages.
    """
    print(f"\n[chunk_search] ① query: '{prompt}'")

    query_embedding = _generate_query_embedding(prompt)
    if query_embedding is None:
        return "Chunk search unavailable: could not generate query embedding."

    try:
        mv   = get_milvus()
        hits = mv.search_chunks(query_embedding, top_k=TOP_K)
    except Exception as e:
        return f"Chunk search unavailable: {e}. Continuing with other search methods."

    print(f"[chunk_search] ② Milvus returned {len(hits)} chunks")

    # Filter by similarity threshold
    hits = [h for h in hits if h["score"] >= MIN_SIMILARITY]
    if not hits:
        return "No relevant passages found (similarity threshold not met). Try rephrasing the query."

    print(f"[chunk_search] ③ After threshold filter: {len(hits)} chunks")
    for h in hits[:5]:
        print(f"  chunk_id={h['chunk_id']} article_id={h['article_id']} score={h['score']:.3f}")

    # Enrich with MySQL metadata (published_date, source_id)
    article_ids = list({h["article_id"] for h in hits})
    db_path     = get_tracking_db_path()
    meta_map    = _get_article_metadata(db_path, article_ids)

    results = _group_and_rerank(hits, meta_map)
    print(f"[chunk_search] ④ Returning {len(results)} articles")
    for r in results:
        print(f"  [{r['similarity']}] {r['title'][:60]} (chunks={r['chunks_used']})")

    if not results:
        return "No articles matched after deduplication."

    summary = (
        f"Found {len(results)} articles via chunk-level semantic search "
        f"(up to {MAX_CHUNKS_PER_ARTICLE} passages per article).\n\n"
    )
    return summary + json.dumps(results, indent=2, ensure_ascii=False)
