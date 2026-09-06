"""answer() = hybrid retrieve (optionally scoped to documents) -> grounded prompt -> LLM
-> answer text + validated citations + all retrieved sources."""
from __future__ import annotations

import logging
import re
import time

from app.config import settings
from app.llm import client as llm
from app.llm.prompts import NOT_FOUND_MARKER, build_messages
from app.models import AskResponse, AskScope, Source
from app.retrieval.fusion import hybrid_retrieve

log = logging.getLogger("rag.answer")

_CITE = re.compile(r"\[(\d+)\]")


def extract_citations(text: str, sources: list[Source]) -> tuple[str, list[Source], list[int]]:
    """Return (cleaned text, cited sources in order of first use, unresolved marker numbers).

    Markers that do not match any retrieved source are removed from the text so the client
    never shows a citation it cannot open."""
    by_n = {s.n: s for s in sources}
    cited: list[Source] = []
    unresolved: list[int] = []
    for m in _CITE.finditer(text):
        n = int(m.group(1))
        if n in by_n:
            if by_n[n] not in cited:
                cited.append(by_n[n])
        elif n not in unresolved:
            unresolved.append(n)
    if unresolved:
        text = _CITE.sub(lambda m: "" if int(m.group(1)) in unresolved else m.group(0), text)
        text = re.sub(r"[ \t]{2,}", " ", text).replace(" .", ".").strip()
    return text, cited, unresolved


def answer(question: str, collection: str | None = None, top_k: int | None = None,
           doc_ids: list[str] | None = None, job_ids: list[str] | None = None) -> AskResponse:
    collection = collection or settings.DEFAULT_COLLECTION
    t0 = time.perf_counter()
    log.info("[ask] collection=%s scope_docs=%s jobs=%s question=%r", collection,
             len(doc_ids) if doc_ids is not None else "all", job_ids or [], question[:120])
    hits = hybrid_retrieve(question, collection, top_k, doc_ids=doc_ids)
    if hits:
        text = llm.chat(build_messages(question, hits))
    else:
        where = "the selected documents" if doc_ids is not None else f"collection '{collection}'"
        text = f"{NOT_FOUND_MARKER} No indexed content was found in {where}."
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
    text, citations, unresolved = extract_citations(text.strip(), sources)
    log.info("[ask] done in %d ms sources=%d citations=%s unresolved=%s not_found=%s",
             (time.perf_counter() - t0) * 1000, len(sources), [c.n for c in citations], unresolved,
             text.startswith(NOT_FOUND_MARKER))
    scope = AskScope(collection=collection, doc_ids=list(doc_ids or []), job_ids=list(job_ids or []),
                     restricted=doc_ids is not None)
    return AskResponse(question=question, answer=text, citations=citations, sources=sources,
                       unresolved_citations=unresolved, scope=scope, collection=collection,
                       model=settings.LLM_MODEL, latency_ms=int((time.perf_counter() - t0) * 1000))
