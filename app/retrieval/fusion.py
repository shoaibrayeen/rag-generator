"""Hybrid retrieval: dense top-k + BM25 top-k, merged with Reciprocal Rank Fusion."""
from __future__ import annotations

import logging

from app.config import settings
from app.retrieval import bm25_index, embedder, vector_store

log = logging.getLogger("rag.fusion")


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


def hybrid_retrieve(question: str, collection: str, top_k: int | None = None,
                    doc_ids: list[str] | None = None) -> list[dict]:
    """doc_ids=None searches the whole collection; a list restricts both retrievers to those documents."""
    top_k = top_k or settings.FUSED_TOP_K
    dense = vector_store.query_dense(collection, embedder.embed_query(question), settings.DENSE_TOP_K,
                                     doc_ids=doc_ids)
    lexical = bm25_index.search(collection, question, settings.BM25_TOP_K,
                                doc_ids=set(doc_ids) if doc_ids is not None else None)

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
    both = sum(1 for r in results if r["dense_rank"] and r["bm25_rank"])
    log.info("[fuse] dense=%d bm25=%d fused_candidates=%d kept=%d in_both=%d rrf_k=%d", len(dense), len(lexical),
             len(fused), len(results), both, settings.RRF_K)
    return results
