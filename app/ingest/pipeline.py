"""Background job: extract -> chunk -> embed -> store -> rebuild BM25.

Status goes QUEUED -> PROCESSING (stage: extracting / chunking / embedding) -> COMPLETED,
or FAILED with a human-readable reason. Every step is logged with job/doc ids and timing
(`grep doc=<id>` in the logs follows one file end to end).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app import jobs
from app.config import settings
from app.ingest.chunker import chunk_pages
from app.ingest.extractors import ExtractionError, extract
from app.logging_utils import ctx, timed
from app.retrieval import bm25_index, embedder, vector_store

log = logging.getLogger("rag.pipeline")


def save_pages(doc_id: str, pages, chunks) -> Path:
    """Write the cleaned page text (the same text the chunk offsets refer to) for the show-page viewer."""
    from app.ingest.chunker import normalise

    settings.pages_dir.mkdir(parents=True, exist_ok=True)
    by_page: dict[int, list[str]] = {}
    for c in chunks:
        by_page.setdefault(int(c.metadata["page"]), []).append(c.chunk_id)
    data = [{"page": pg.page_no, "section": pg.section, "extraction": pg.extraction, "text": normalise(pg.text),
             "chunk_ids": by_page.get(pg.page_no, [])} for pg in pages]
    out = settings.pages_dir / f"{doc_id}.json"
    out.write_text(json.dumps(data, ensure_ascii=False))
    log.debug("[store] pages json written %s pages=%d", out, len(data))
    return out


def run(doc_id: str, path: Path) -> None:
    doc = jobs.get(doc_id)
    if doc is None or doc.status != "QUEUED":
        log.warning("[pipeline] skip %s: not QUEUED (%s)", doc_id, doc.status if doc else "missing")
        return
    ids = dict(job=doc.job_id, doc=doc_id, file=doc.filename)
    log.info("[pipeline] begin %s type=%s collection=%s size=%dB", ctx(**ids), doc.file_type,
             doc.collection, path.stat().st_size if path.exists() else -1)
    try:
        jobs.update(doc_id, status="PROCESSING", stage="extracting", reason="Extracting text")
        with timed(log, "extract", **ids):
            pages = extract(path)  # raises ExtractionError with a user-facing reason
        ocr_pages = sum(1 for p in pages if p.extraction == "ocr")
        note = next((p.meta.get("format_note") for p in pages if p.meta.get("format_note")), None)
        log.info("[extract] %s pages=%d ocr_pages=%d chars=%d%s", ctx(**ids), len(pages), ocr_pages,
                 sum(len(p.text) for p in pages), f" note={note}" if note else "")

        jobs.update(doc_id, stage="chunking", pages=len(pages), ocr_pages=ocr_pages,
                    reason=f"Chunking {len(pages)} pages")
        with timed(log, "chunk", **ids):
            chunks = chunk_pages(pages, doc_id=doc_id, source=doc.filename, file_type=doc.file_type or "",
                                 collection=doc.collection, job_id=doc.job_id)
        if not chunks:
            raise ValueError("Extraction produced no usable text.")
        log.info("[chunk] %s chunks=%d avg_chars=%d", ctx(**ids), len(chunks),
                 sum(len(c.text) for c in chunks) // len(chunks))

        jobs.update(doc_id, stage="embedding", chunks=len(chunks), reason=f"Embedding {len(chunks)} chunks")
        with timed(log, "embed", **ids):
            vectors = embedder.embed_texts([c.text for c in chunks])
        with timed(log, "store", **ids):
            vector_store.delete_document(doc.collection, doc_id)  # a retry must replace, never duplicate, chunks
            vector_store.add_chunks([c.chunk_id for c in chunks], [c.text for c in chunks], vectors,
                                    [c.metadata for c in chunks])
            save_pages(doc_id, pages, chunks)
        with timed(log, "bm25-rebuild", collection=doc.collection, **ids):
            n = bm25_index.rebuild(doc.collection)
        log.info("[bm25-rebuild] %s collection=%s indexed_chunks=%d", ctx(**ids), doc.collection, n)

        ocr_note = f", {ocr_pages} via OCR" if ocr_pages else ""
        fmt_note = f" ({note})" if note else ""
        jobs.update(doc_id, status="COMPLETED", stage=None,
                    reason=f"Indexed {len(pages)} pages{ocr_note} into {len(chunks)} chunks{fmt_note}")
        log.info("[pipeline] COMPLETED %s", ctx(**ids))
    except ExtractionError as exc:
        log.warning("[pipeline] FAILED %s: %s", ctx(**ids), exc)
        jobs.update(doc_id, status="FAILED", stage=None, reason=f"PROCESSING FAILED: {exc}")
    except Exception as exc:
        log.exception("[pipeline] FAILED %s", ctx(**ids))
        jobs.update(doc_id, status="FAILED", stage=None,
                    reason=f"PROCESSING FAILED: {type(exc).__name__}: {exc}")
