"""fastembed wrapper. One model instance per process, loaded lazily."""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings

log = logging.getLogger("rag.embedder")

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                t0 = time.perf_counter()
                log.info("[embedder] loading model %s", settings.EMBEDDING_MODEL)
                _model = TextEmbedding(model_name=settings.EMBEDDING_MODEL)
                log.info("[embedder] model ready in %d ms", (time.perf_counter() - t0) * 1000)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_model()
    t0 = time.perf_counter()
    out = [vec.tolist() for vec in model.embed(texts, batch_size=settings.EMBEDDING_BATCH_SIZE)]
    log.info("[embed] texts=%d batch_size=%d dims=%d in %d ms", len(texts), settings.EMBEDDING_BATCH_SIZE,
             len(out[0]) if out else 0, (time.perf_counter() - t0) * 1000)
    return out


def embed_query(text: str) -> list[float]:
    model = get_model()
    # bge models expose a query-specific path; fall back to plain embed otherwise.
    if hasattr(model, "query_embed"):
        return next(iter(model.query_embed([text]))).tolist()
    return embed_texts([text])[0]
