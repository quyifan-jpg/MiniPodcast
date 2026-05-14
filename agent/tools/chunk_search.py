"""
Chunk-level semantic search tool for the podcast agent.

Instead of returning whole articles, this tool returns the most relevant
*passages* from articles, giving the script agent much richer and more
targeted context.
"""

import os
import json
import numpy as np
from agno.agent import Agent
from openai import OpenAI
from db.config import get_tracking_db_path, get_chunk_faiss_db_path
from db.connection import execute_query
from utils.load_api_keys import load_api_key

EMBEDDING_MODEL = "text-embedding-3-small"
TOP_K = 30  # how many chunks to retrieve from FAISS
MAX_CHUNKS_PER_ARTICLE = 3  # deduplicate: keep at most N chunks per article
MIN_SIMILARITY = 0.75  # cosine-sim threshold (lower than article search since chunks are smaller)
FINAL_TOP_K = 8  # number of articles (with passages) to return to the agent


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


def _search_faiss(query_embedding: list[float], index_path: str, mapping_path: str, top_k: int) -> list[tuple[int, float]]:
    """Returns [(chunk_id, similarity), ...] sorted by similarity desc."""
    import faiss

    if not os.path.exists(index_path) or not os.path.exists(mapping_path):
        return []
    try:
        index = faiss.read_index(index_path)
        id_map = np.load(mapping_path).tolist()
    except Exception as e:
        print(f"chunk_search: FAISS load error: {e}")
        return []

    query_vec = np.array([query_embedding], dtype=np.float32)
    distances, indices = index.search(query_vec, top_k)

    results = []
    for dist, idx in zip(distances[0], indices[0]):
        if idx < 0 or idx >= len(id_map):
            continue
        similarity = float(np.exp(-dist)) if dist > 0 else 1.0
        if similarity >= MIN_SIMILARITY:
            results.append((id_map[idx], similarity))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _fetch_chunks(db_path: str, chunk_ids: list[int]) -> dict[int, dict]:
    """Fetch chunk rows by id. Returns {chunk_id: row}."""
    if not chunk_ids:
        return {}
    placeholders = ",".join(["?"] * len(chunk_ids))
    rows = execute_query(
        db_path,
        f"SELECT id, article_id, chunk_index, chunk_text FROM article_chunks WHERE id IN ({placeholders})",
        chunk_ids,
        fetch=True,
    )
    return {row["id"]: row for row in rows} if rows else {}


def _fetch_articles(db_path: str, article_ids: list[int]) -> dict[int, dict]:
    """Fetch article metadata by id. Returns {article_id: row}."""
    if not article_ids:
        return {}
    placeholders = ",".join(["?"] * len(article_ids))
    rows = execute_query(
        db_path,
        f"""SELECT id, title, url, published_date, source_id
            FROM crawled_articles WHERE id IN ({placeholders})""",
        article_ids,
        fetch=True,
    )
    return {row["id"]: row for row in rows} if rows else {}


def _group_and_rerank(
    scored_chunks: list[tuple[int, float]],
    chunk_map: dict[int, dict],
    article_map: dict[int, dict],
) -> list[dict]:
    """
    Group top chunks by article, deduplicate, assemble passage context.
    Returns a list of article-level result dicts.
    """
    # article_id → [(chunk_row, similarity)]
    by_article: dict[int, list] = {}
    for chunk_id, sim in scored_chunks:
        chunk = chunk_map.get(chunk_id)
        if not chunk:
            continue
        aid = chunk["article_id"]
        if aid not in by_article:
            by_article[aid] = []
        if len(by_article[aid]) < MAX_CHUNKS_PER_ARTICLE:
            by_article[aid].append((chunk, sim))

    results = []
    for article_id, chunk_sims in by_article.items():
        article = article_map.get(article_id)
        if not article:
            continue

        # Sort chunks by their position in the article for readability
        chunk_sims.sort(key=lambda x: x[0]["chunk_index"])

        # Best similarity score for this article (for final ranking)
        best_sim = max(s for _, s in chunk_sims)

        # Assemble relevant passages
        passages = "\n\n---\n\n".join(c["chunk_text"] for c, _ in chunk_sims)

        results.append(
            {
                "id": article_id,
                "title": article.get("title", "Untitled"),
                "url": article.get("url", ""),
                "published_date": article.get("published_date", ""),
                "source_id": str(article.get("source_id", "")),
                # Script agent reads full_text; give it the relevant passages only
                "full_text": passages,
                "description": passages,
                "similarity": round(best_sim, 3),
                "chunks_used": len(chunk_sims),
                "is_scrapping_required": False,
                "categories": ["chunk-semantic"],
            }
        )

    # Final ranking: by best similarity score
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results[:FINAL_TOP_K]


def chunk_search(agent: Agent, prompt: str) -> str:
    """
    Semantic chunk-level search over internal articles.

    Retrieves the most relevant *passages* (not whole articles) from the
    knowledge base using vector similarity, then returns them grouped by
    article so the script agent can generate a richer, more grounded podcast.

    Args:
        agent:  Agno agent instance
        prompt: Search query describing the podcast topic

    Returns:
        JSON string with top articles and their most relevant passages.
    """
    print(f"chunk_search: query='{prompt}'")

    db_path = get_tracking_db_path()
    index_path, mapping_path = get_chunk_faiss_db_path()

    if not os.path.exists(index_path):
        return "Chunk index not available yet. Run `python -m processors.chunk_processor` first, then retry."

    query_embedding = _generate_query_embedding(prompt)
    if query_embedding is None:
        return "Chunk search unavailable: could not generate query embedding."

    scored_chunks = _search_faiss(query_embedding, index_path, mapping_path, TOP_K)
    if not scored_chunks:
        return "No relevant passages found (similarity threshold not met). Try rephrasing the query."

    chunk_ids = [cid for cid, _ in scored_chunks]
    chunk_map = _fetch_chunks(db_path, chunk_ids)
    article_ids = list({c["article_id"] for c in chunk_map.values()})
    article_map = _fetch_articles(db_path, article_ids)

    results = _group_and_rerank(scored_chunks, chunk_map, article_map)

    if not results:
        return "No articles matched after deduplication."

    summary = (
        f"Found {len(results)} articles via chunk-level semantic search. "
        f"Each result contains only the most relevant passages (up to {MAX_CHUNKS_PER_ARTICLE} per article).\n\n"
    )
    return summary + json.dumps(results, indent=2, ensure_ascii=False)
