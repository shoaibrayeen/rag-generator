"""answer() = hybrid retrieve -> grounded prompt -> LLM -> answer + cited sources."""
from __future__ import annotations

import time

from app.config import settings
from app.llm import client as llm
from app.llm.prompts import NOT_FOUND_MARKER, build_messages
from app.models import AskResponse, Source
from app.retrieval.fusion import hybrid_retrieve


def answer(question: str, collection: str | None = None, top_k: int | None = None) -> AskResponse:
    collection = collection or settings.DEFAULT_COLLECTION
    t0 = time.perf_counter()
    hits = hybrid_retrieve(question, collection, top_k)
    if hits:
        text = llm.chat(build_messages(question, hits))
    else:
        text = f"{NOT_FOUND_MARKER} No documents are indexed in collection '{collection}'."
    sources = [
        Source(
            n=i,
            chunk_id=h["chunk_id"],
            doc_id=h["metadata"].get("doc_id", ""),
            source=h["metadata"].get("source", ""),
            file_type=h["metadata"].get("file_type", ""),
            page=int(h["metadata"].get("page", 0)),
            section=h["metadata"].get("section") or None,
            extraction=h["metadata"].get("extraction", "text"),
            text=h["text"],
            dense_rank=h.get("dense_rank"),
            bm25_rank=h.get("bm25_rank"),
            rrf_score=h["rrf_score"],
        )
        for i, h in enumerate(hits, start=1)
    ]
    return AskResponse(question=question, answer=text.strip(), sources=sources, collection=collection,
                       model=settings.LLM_MODEL, latency_ms=int((time.perf_counter() - t0) * 1000))
