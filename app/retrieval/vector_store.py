"""ChromaDB (embedded PersistentClient) wrapper. One collection per document set."""
from __future__ import annotations

import threading

import chromadb

from app.config import settings

_client = None
_lock = threading.Lock()


def client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                settings.chroma_dir.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return _client


def get_collection(name: str):
    return client().get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def list_collections() -> list[str]:
    cols = client().list_collections()
    return sorted(c.name if hasattr(c, "name") else str(c) for c in cols)


def add_chunks(collection: str, ids: list[str], texts: list[str],
               embeddings: list[list[float]], metadatas: list[dict]) -> None:
    if not ids:
        return
    col = get_collection(collection)
    # Chroma has a per-call batch limit; stay well under it.
    step = 500
    for i in range(0, len(ids), step):
        col.add(ids=ids[i:i + step], documents=texts[i:i + step],
                embeddings=embeddings[i:i + step], metadatas=metadatas[i:i + step])


def query_dense(collection: str, query_embedding: list[float], top_k: int,
                doc_ids: list[str] | None = None) -> list[dict]:
    col = get_collection(collection)
    total = col.count()
    if total == 0 or doc_ids == []:
        return []
    kwargs = {}
    if doc_ids:
        kwargs["where"] = {"doc_id": {"$in": list(doc_ids)}}
        total = min(total, col.count() if len(doc_ids) > 50 else
                    len(col.get(where=kwargs["where"], include=[])["ids"]))
        if total == 0:
            return []
    res = col.query(query_embeddings=[query_embedding], n_results=min(top_k, total),
                    include=["documents", "metadatas", "distances"], **kwargs)
    out = []
    for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                    res["metadatas"][0], res["distances"][0]):
        out.append({"chunk_id": cid, "text": doc, "metadata": meta, "distance": dist})
    return out


def get_all_chunks(collection: str) -> list[dict]:
    col = get_collection(collection)
    total = col.count()
    out: list[dict] = []
    step = 1000
    for offset in range(0, total, step):
        res = col.get(include=["documents", "metadatas"], limit=step, offset=offset)
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
            out.append({"chunk_id": cid, "text": doc, "metadata": meta})
    return out


def delete_document(collection: str, doc_id: str) -> None:
    col = get_collection(collection)
    col.delete(where={"doc_id": doc_id})


def delete_collection(collection: str) -> None:
    try:
        client().delete_collection(collection)
    except Exception:
        pass


def count(collection: str) -> int:
    return get_collection(collection).count()
