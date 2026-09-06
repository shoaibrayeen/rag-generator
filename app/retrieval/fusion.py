"""Hybrid retrieval: dense top-k + BM25 top-k, merged with Reciprocal Rank Fusion."""
from __future__ import annotations

from app.config import settings
from app.retrieval import bm25_index, embedder, vector_store


def rrf_merge(ranked_lists: dict[str, list[str]], k: int) -> list[tuple[str, float, dict[str, int]]]:
    """
    ranked_lists: {"dense": [chunk_id,...], "bm25": [chunk_id,...]} in rank order.
    Returns [(chunk_id, rrf_score, {retriever: rank}), ...] sorted by score desc.
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in ranked_lists.items():
        for rank, cid in enumerate(ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(cid, {})[name] = rank
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(cid, score, ranks[cid]) for cid, score in ordered]


def hybrid_retrieve(question: str, collection: str, top_k: int | None = None) -> list[dict]:
    top_k = top_k or settings.FUSED_TOP_K
    dense = vector_store.query_dense(collection, embedder.embed_query(question), settings.DENSE_TOP_K)
    lexical = bm25_index.search(collection, question, settings.BM25_TOP_K)

    by_id: dict[str, dict] = {}
    for hit in dense + lexical:
        by_id.setdefault(hit["chunk_id"], {"chunk_id": hit["chunk_id"], "text": hit["text"],
                                           "metadata": hit["metadata"]})

    fused = rrf_merge({"dense": [h["chunk_id"] for h in dense],
                       "bm25": [h["chunk_id"] for h in lexical]}, settings.RRF_K)

    results = []
    for cid, score, ranks in fused[:top_k]:
        item = dict(by_id[cid])
        item["rrf_score"] = round(score, 6)
        item["dense_rank"] = ranks.get("dense")
        item["bm25_rank"] = ranks.get("bm25")
        results.append(item)
    return results
