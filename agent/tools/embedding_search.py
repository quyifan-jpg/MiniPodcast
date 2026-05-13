"""
Article-level semantic search backed by Milvus.

Queries the `article_vectors` Milvus collection and enriches
results with full article metadata from MySQL.
"""

import json

from agno.agent import Agent
from openai import OpenAI

from db.config import get_tracking_db_path
from db.connection import execute_query
from db.milvus import get_milvus
from utils.load_api_keys import load_api_key

EMBEDDING_MODEL      = "text-embedding-3-small"
TOP_K                = 20
SIMILARITY_THRESHOLD = 0.40   # IP score threshold (OpenAI embeddings: ≥0.4 ≈ strong match)


def _generate_query_embedding(query: str) -> list[float] | None:
    api_key = load_api_key("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        client = OpenAI(api_key=api_key)
        resp = client.embeddings.create(input=query, model=EMBEDDING_MODEL)
        return resp.data[0].embedding
    except Exception as e:
        print(f"embedding_search: embedding error: {e}")
        return None


def _get_article_details(db_path: str, article_ids: list[int]) -> dict[int, dict]:
    if not article_ids:
        return {}
    placeholders = ",".join(["%s"] * len(article_ids))
    rows = execute_query(
        db_path,
        f"""SELECT id, title, url, published_date, summary, content, source_id
            FROM crawled_articles WHERE id IN ({placeholders})""",
        article_ids,
        fetch=True,
    )
    return {row["id"]: row for row in rows} if rows else {}


def embedding_search(agent: Agent, prompt: str) -> str:
    """
    Semantic search over internal articles using Milvus vector similarity.

    Finds articles semantically similar to the query (cosine similarity ≥ 40%).
    Use this as the primary internal knowledge source before external searches.

    Args:
        agent:  Agno agent instance
        prompt: Search query describing the podcast topic

    Returns:
        JSON string with matched articles and similarity scores.
    """
    print(f"[embedding_search] query: '{prompt}'")

    query_embedding = _generate_query_embedding(prompt)
    if query_embedding is None:
        return "Embedding search unavailable: could not generate query embedding. Continuing with other search methods."

    try:
        mv      = get_milvus()
        hits    = mv.search_articles(query_embedding, top_k=TOP_K)
        db_path = get_tracking_db_path()
    except Exception as e:
        return f"Embedding search unavailable: {e}. Continuing with other search methods."

    # Filter by similarity threshold
    hits = [h for h in hits if h["score"] >= SIMILARITY_THRESHOLD]
    if not hits:
        return "No high-quality semantic matches found (threshold: 40%). Continuing with other search methods."

    article_ids  = [h["article_id"] for h in hits]
    article_map  = _get_article_details(db_path, article_ids)
    score_map    = {h["article_id"]: h["score"] for h in hits}

    results = []
    for aid in article_ids:
        art   = article_map.get(aid)
        if not art:
            continue
        score = score_map.get(aid, 0.0)
        results.append({
            "id":             aid,
            "title":          f"{art.get('title', 'Untitled')} (Relevance: {int(score * 100)}%)",
            "url":            art.get("url", "#"),
            "published_date": str(art.get("published_date", "")),
            "description":    art.get("summary") or art.get("content") or "",
            "source_id":      str(art.get("source_id", "")),
            "similarity":     round(score, 3),
            "categories":     ["semantic"],
            "is_scrapping_required": False,
        })

    print(f"[embedding_search] returning {len(results)} results")
    return f"Found {len(results)} results: {json.dumps(results, indent=2, ensure_ascii=False)}"
