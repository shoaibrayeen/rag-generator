"""ChromaDB (embedded PersistentClient) wrapper.

All chunks live in ONE Chroma collection, `settings.CHUNKS_STORE` ("document_chunks").
Every chunk's metadata carries `collection` (the user-facing document set), `doc_id` and
`job_id`, so document sets, documents and upload jobs are all metadata filters.
"""
from __future__ import annotations

import logging
import threading

import chromadb

from app.config import settings

log = logging.getLogger("rag.vector_store")

_client = None
_lock = threading.Lock()


def client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                settings.chroma_dir.mkdir(parents=True, exist_ok=True)
                log.info("[vector_store] opening Chroma at %s store=%s", settings.chroma_dir, settings.CHUNKS_STORE)
                _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return _client


def store():
    return client().get_or_create_collection(name=settings.CHUNKS_STORE, metadata={"hnsw:space": "cosine"})


def _where(collection: str, doc_ids: list[str] | None = None, job_id: str | None = None) -> dict:
    clauses: list[dict] = [{"collection": collection}]
    if doc_ids is not None:
        clauses.append({"doc_id": {"$in": list(doc_ids) or ["__none__"]}})
    if job_id:
        clauses.append({"job_id": job_id})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def add_chunks(ids: list[str], texts: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None:
    if not ids:
        return
    col = store()
    step = 500  # stay under Chroma's per-call batch limit
    for i in range(0, len(ids), step):
        col.add(ids=ids[i:i + step], documents=texts[i:i + step],
                embeddings=embeddings[i:i + step], metadatas=metadatas[i:i + step])
        log.debug("[store] added batch %d-%d", i, min(i + step, len(ids)))
    log.info("[store] added chunks=%d doc=%s collection=%s", len(ids), metadatas[0].get("doc_id"),
             metadatas[0].get("collection"))


def count(collection: str, doc_ids: list[str] | None = None, job_id: str | None = None) -> int:
    return len(store().get(where=_where(collection, doc_ids, job_id), include=[])["ids"])


def query_dense(collection: str, query_embedding: list[float], top_k: int,
                doc_ids: list[str] | None = None) -> list[dict]:
    if doc_ids == []:
        return []
    where = _where(collection, doc_ids)
    total = count(collection, doc_ids)
    if total == 0:
        return []
    res = store().query(query_embeddings=[query_embedding], n_results=min(top_k, total), where=where,
                        include=["documents", "metadatas", "distances"])
    log.info("[retrieve-dense] collection=%s scope_docs=%s candidates=%d hits=%d", collection,
             len(doc_ids) if doc_ids is not None else "all", total, len(res["ids"][0]))
    return [{"chunk_id": cid, "text": doc, "metadata": meta, "distance": dist}
            for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                            res["metadatas"][0], res["distances"][0])]


def get_all_chunks(collection: str, doc_ids: list[str] | None = None, job_id: str | None = None) -> list[dict]:
    col = store()
    where = _where(collection, doc_ids, job_id)
    out: list[dict] = []
    step = 1000
    offset = 0
    while True:
        res = col.get(where=where, include=["documents", "metadatas"], limit=step, offset=offset)
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
            out.append({"chunk_id": cid, "text": doc, "metadata": meta})
        if len(res["ids"]) < step:
            break
        offset += step
    out.sort(key=lambda c: (c["metadata"].get("doc_id", ""), int(c["metadata"].get("chunk_index", 0))))
    return out


def list_collections() -> list[str]:
    """Distinct document sets that currently have chunks."""
    col = store()
    seen: set[str] = set()
    offset, step = 0, 5000
    while True:
        res = col.get(include=["metadatas"], limit=step, offset=offset)
        for meta in res["metadatas"]:
            if meta and meta.get("collection"):
                seen.add(meta["collection"])
        if len(res["ids"]) < step:
            break
        offset += step
    return sorted(seen)


def delete_document(collection: str, doc_id: str) -> None:
    n = count(collection, [doc_id])
    store().delete(where=_where(collection, [doc_id]))
    log.info("[store] deleted chunks=%d doc=%s collection=%s", n, doc_id, collection)


def delete_collection(collection: str) -> None:
    if count(collection):
        store().delete(where=_where(collection))
