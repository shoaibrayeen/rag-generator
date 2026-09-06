"""rank-bm25 lexical index, one per collection, rebuilt from Chroma."""
from __future__ import annotations

import re
import threading

from rank_bm25 import BM25Okapi

from app.retrieval import vector_store

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were",
    "be", "it", "this", "that", "with", "as", "by", "at", "from", "what", "which", "who",
    "how", "does", "do", "did", "can", "i", "you", "we", "they", "he", "she",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


class _Index:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        corpus = [tokenize(c["text"]) or ["_"] for c in chunks]
        self.bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int, doc_ids: set[str] | None = None) -> list[dict]:
        if not self.bm25:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        candidates = range(len(scores))
        if doc_ids is not None:
            candidates = [i for i in candidates if self.chunks[i]["metadata"].get("doc_id") in doc_ids]
        ranked = sorted(candidates, key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                break
            c = self.chunks[i]
            out.append({**c, "bm25_score": float(scores[i])})
        return out


_indexes: dict[str, _Index] = {}
_lock = threading.Lock()


def rebuild(collection: str) -> int:
    chunks = vector_store.get_all_chunks(collection)
    with _lock:
        _indexes[collection] = _Index(chunks)
    return len(chunks)


def drop(collection: str) -> None:
    with _lock:
        _indexes.pop(collection, None)


def search(collection: str, query: str, top_k: int, doc_ids: set[str] | None = None) -> list[dict]:
    idx = _indexes.get(collection)
    if idx is None:
        rebuild(collection)
        idx = _indexes.get(collection)
    return idx.search(query, top_k, doc_ids) if idx else []
