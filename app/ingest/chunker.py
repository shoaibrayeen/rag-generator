"""
Overlapping, metadata-tagged chunking.

Each page is split into windows of `CHUNK_SIZE_CHARS` with `CHUNK_OVERLAP_CHARS`
of overlap. Cuts prefer paragraph, then sentence, then word boundaries so chunks
read naturally. Every chunk carries the metadata needed to cite it later.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.ingest.extractors import Page

log = logging.getLogger("rag.chunker")

_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


@dataclass
class Chunk:
    chunk_id: str
    text: str
    metadata: dict


def _find_cut(text: str, start: int, hard_end: int) -> int:
    """Pick the best boundary in the last 25% of the window, else hard cut."""
    if hard_end >= len(text):
        return len(text)
    window_start = start + int((hard_end - start) * 0.75)
    for sep in _BOUNDARIES:
        idx = text.rfind(sep, window_start, hard_end)
        if idx != -1:
            return idx + len(sep)
    return hard_end


def split_text(text: str, size: int | None = None, overlap: int | None = None) -> list[tuple[int, int]]:
    """Return (start, end) char offsets of overlapping windows over `text`."""
    size = size or settings.CHUNK_SIZE_CHARS
    overlap = settings.CHUNK_OVERLAP_CHARS if overlap is None else overlap
    if size <= 0:
        raise ValueError("chunk size must be positive")
    overlap = min(max(0, overlap), size - 1)
    spans: list[tuple[int, int]] = []
    n = len(text)
    start = 0
    while start < n:
        end = _find_cut(text, start, min(start + size, n))
        if end <= start:  # safety: always make progress
            end = min(start + size, n)
        spans.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return spans


def chunk_pages(pages: list[Page], *, doc_id: str, source: str, file_type: str,
                collection: str, job_id: str = "") -> list[Chunk]:
    chunks: list[Chunk] = []
    idx = 0
    for page in pages:
        text = re.sub(r"[ \t]+\n", "\n", page.text).strip()
        if not text:
            continue
        for start, end in split_text(text):
            piece = text[start:end].strip()
            if not piece:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}:{idx}",
                    text=piece,
                    metadata={
                        "doc_id": doc_id,
                        "job_id": job_id,
                        "source": source,
                        "file_type": file_type,
                        "collection": collection,
                        "page": page.page_no,
                        "section": page.section or "",
                        "extraction": page.extraction,
                        "chunk_index": idx,
                        "char_start": start,
                        "char_end": end,
                    },
                )
            )
            idx += 1
    log.debug("[chunk] doc=%s pages=%d chunks=%d size=%d overlap=%d", doc_id, len(pages), len(chunks),
              settings.CHUNK_SIZE_CHARS, settings.CHUNK_OVERLAP_CHARS)
    return chunks
