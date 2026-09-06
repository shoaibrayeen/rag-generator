"""Background job: extract -> chunk -> embed -> store -> rebuild BM25.

Status goes QUEUED -> PROCESSING (stage: extracting / chunking / embedding) -> COMPLETED,
or FAILED with a human-readable reason. Runs in FastAPI's background thread pool.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app import jobs
from app.ingest.chunker import chunk_pages
from app.ingest.extractors import ExtractionError, extract
from app.retrieval import bm25_index, embedder, vector_store

log = logging.getLogger(__name__)


def run(doc_id: str, path: Path) -> None:
    doc = jobs.get(doc_id)
    if doc is None or doc.status != "QUEUED":
        return
    try:
        jobs.update(doc_id, status="PROCESSING", stage="extracting", reason="Extracting text")
        pages = extract(path)  # raises ExtractionError with a user-facing reason
        ocr_pages = sum(1 for p in pages if p.extraction == "ocr")

        jobs.update(doc_id, stage="chunking", pages=len(pages), ocr_pages=ocr_pages,
                    reason=f"Chunking {len(pages)} pages")
        chunks = chunk_pages(pages, doc_id=doc_id, source=doc.filename, file_type=doc.file_type or "",
                             collection=doc.collection, job_id=doc.job_id)
        if not chunks:
            raise ValueError("Extraction produced no usable text.")

        jobs.update(doc_id, stage="embedding", chunks=len(chunks),
                    reason=f"Embedding {len(chunks)} chunks")
        vectors = embedder.embed_texts([c.text for c in chunks])
        vector_store.add_chunks([c.chunk_id for c in chunks], [c.text for c in chunks], vectors,
                                [c.metadata for c in chunks])
        bm25_index.rebuild(doc.collection)

        ocr_note = f", {ocr_pages} via OCR" if ocr_pages else ""
        jobs.update(doc_id, status="COMPLETED", stage=None,
                    reason=f"Indexed {len(pages)} pages{ocr_note} into {len(chunks)} chunks")
        log.info("COMPLETED %s (%s)", doc.filename, doc_id)
    except ExtractionError as exc:
        log.warning("FAILED %s (%s): %s", doc.filename, doc_id, exc)
        jobs.update(doc_id, status="FAILED", stage=None, reason=f"PROCESSING FAILED: {exc}")
    except Exception as exc:
        log.exception("FAILED %s (%s)", doc.filename, doc_id)
        jobs.update(doc_id, status="FAILED", stage=None,
                    reason=f"PROCESSING FAILED: {type(exc).__name__}: {exc}")
