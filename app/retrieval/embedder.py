"""fastembed wrapper. One model instance per process, loaded lazily."""
from __future__ import annotations

import threading

from app.config import settings

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name=settings.EMBEDDING_MODEL)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_model()
    return [vec.tolist() for vec in model.embed(texts, batch_size=settings.EMBEDDING_BATCH_SIZE)]


def embed_query(text: str) -> list[float]:
    model = get_model()
    # bge models expose a query-specific path; fall back to plain embed otherwise.
    if hasattr(model, "query_embed"):
        return next(iter(model.query_embed([text]))).tolist()
    return embed_texts([text])[0]
