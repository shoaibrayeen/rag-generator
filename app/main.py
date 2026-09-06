"""FastAPI application: REST API + single-page UI + documentation pages.

Upload is asynchronous: POST /api/documents stores the files of ONE job, returns the job
(with one document record per file) immediately (202), and a background worker indexes
them. Poll GET /api/jobs/{job_id}. Asking can be scoped to selected documents or jobs.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import get_args

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import jobs
from app.config import SUPPORTED_EXTENSIONS, settings
from app.ingest import pipeline
from app.ingest.extractors import file_type_for, ocr_available
from app.llm.client import LLMError
from app.models import (TERMINAL_STATUSES, AskRequest, AskResponse, ChunkOut, DocStatus, DocumentInfo,
                        DocumentPage, DocumentPages, HealthResponse, JobDetail, JobPage, PageOut, UploadResponse)
from app.rag import answer
from app.retrieval import bm25_index, embedder, vector_store

from app.logging_utils import configure_logging, ctx

configure_logging()
log = logging.getLogger("rag.api")

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).parent / "static"
DOCS_DIR = ROOT / "documentation"
# Mirrors ChromaDB's collection-name rule: 3-63 chars, [a-zA-Z0-9._-], alphanumeric at both ends.
_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$")


def _paginate(items: list, page: int, size: int) -> dict:
    """0-based page slicing shared by the job and document listings."""
    total = len(items)
    pages = (total + size - 1) // size if total else 0
    start = page * size
    return {"items": items[start:start + size], "page": page, "size": size, "total": total, "pages": pages,
            "has_next": start + size < total, "has_prev": page > 0 and total > 0}


def _collection(name: str | None) -> str:
    name = (name or settings.DEFAULT_COLLECTION).strip()
    if not _COLLECTION_RE.match(name):
        raise HTTPException(400, "Collection name must be 3-63 chars of letters, digits, . _ - and start/end with a letter or digit")
    return name


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    log.info("warming embedding model %s", settings.EMBEDDING_MODEL)
    embedder.get_model()
    for col in vector_store.list_collections():
        n = bm25_index.rebuild(col)
        log.info("BM25 rebuilt for '%s' (%d chunks)", col, n)
    log.info("OCR available: %s | LLM: %s @ %s", ocr_available(), settings.LLM_MODEL, settings.LLM_API_URL)
    yield


app = FastAPI(title="RAG Generator", version="0.9.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if DOCS_DIR.exists():
    app.mount("/documentation", StaticFiles(directory=DOCS_DIR), name="documentation")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/jobs", include_in_schema=False)
def jobs_page():
    """Dedicated job listing page (20 per page)."""
    return FileResponse(STATIC_DIR / "jobs.html")


@app.get("/documents", include_in_schema=False)
def documents_page():
    """Dedicated document listing page (20 per page, filter by ?job_id=&status=&collection=)."""
    return FileResponse(STATIC_DIR / "documents.html")


@app.get("/documents/{doc_id}", include_in_schema=False)
def document_show_page(doc_id: str):
    """Show page for one document: viewer for COMPLETED, explanation otherwise."""
    return FileResponse(STATIC_DIR / "document.html")


@app.get("/api/health", response_model=HealthResponse)
def health():
    public = {k: v for k, v in settings.model_dump().items() if "KEY" not in k}
    public = {k: (str(v) if isinstance(v, Path) else v) for k, v in public.items()}
    return HealthResponse(
        status="ok", llm_model=settings.LLM_MODEL, llm_api_url=settings.LLM_API_URL,
        embedding_model=settings.EMBEDDING_MODEL, supported_extensions=sorted(SUPPORTED_EXTENSIONS),
        ocr_enabled=settings.OCR_ENABLED, ocr_available=ocr_available(),
        collections=sorted(set(vector_store.list_collections()) | set(jobs.collections())),
        statuses=list(get_args(DocStatus)), settings=public,
    )


# ----------------------------------------------------------------------- upload (async)

@app.post("/api/documents", response_model=UploadResponse, status_code=202)
async def upload(background: BackgroundTasks, files: list[UploadFile] = File(...),
                 collection: str | None = Form(default=None)):
    """Accept files as ONE job; return the job (with a record per file) immediately.

    Per file: validate (extension, size) -> upload step: store the file and compute its SHA-256
    -> duplicate check against live documents of the same collection -> QUEUED. Every file gets a
    document record, even rejected ones (EXTRACTION_NOT_SUPPORTED, EMPTY_FILE, UPLOAD_FAILED,
    DUPLICATE), so the listing explains what happened. Poll GET /api/jobs/{job_id}.
    """
    col = _collection(collection)
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    job_id = uuid.uuid4().hex[:12]
    jobs.create_job(job_id, col)

    log.info("[upload] job=%s collection=%s files=%d", job_id, col, len(files))

    def record(doc_id: str, name: str, status: str, **kw) -> DocumentInfo:
        log.info("[upload] %s -> %s%s", ctx(job=job_id, doc=doc_id, file=name), status,
                 f" ({kw['reason']})" if status != "QUEUED" and kw.get("reason") else "")
        return jobs.create(doc_id, name, col, status, job_id=job_id, **kw)

    for f in files:
        name = Path(f.filename or "upload").name
        doc_id = uuid.uuid4().hex[:12]
        kind = file_type_for(name)

        try:
            data = await f.read()
        except Exception as exc:
            record(doc_id, name, "UPLOAD_FAILED", file_type=kind,
                   reason=f"UPLOAD FAILED: could not read upload stream ({exc})")
            continue

        sha = hashlib.sha256(data).hexdigest() if data else None  # computed at upload time, kept on every record

        if kind is None:
            record(doc_id, name, "EXTRACTION_NOT_SUPPORTED", sha256=sha,
                   reason=f"Extension '{Path(name).suffix or '(none)'}' is not supported. "
                          f"Supported: {' '.join(sorted(SUPPORTED_EXTENSIONS))}")
            continue
        if not data:
            record(doc_id, name, "EMPTY_FILE", file_type=kind, reason="The uploaded file is 0 bytes")
            continue
        if len(data) > limit:
            record(doc_id, name, "UPLOAD_FAILED", file_type=kind, sha256=sha,
                   reason=f"UPLOAD FAILED: file is larger than {settings.MAX_UPLOAD_MB} MB")
            continue

        # 1. upload step: store the file
        path = settings.uploads_dir / f"{doc_id}_{name}"
        try:
            path.write_bytes(data)
        except OSError as exc:
            record(doc_id, name, "UPLOAD_FAILED", file_type=kind, sha256=sha,
                   reason=f"UPLOAD FAILED: could not store file ({exc})")
            continue
        log.debug("[upload] stored %s bytes=%d sha256=%s", ctx(job=job_id, doc=doc_id, file=name), len(data), sha)

        # 2. duplicate check on the stored file's hash (same collection, live original)
        original = jobs.find_by_hash(col, sha)
        if original is not None:
            record(doc_id, name, "DUPLICATE", file_type=kind, sha256=sha, duplicate_of=original,
                   reason=f"Same content as '{original.filename}' ({original.doc_id}); not processed again")
            continue

        # 3. queue for processing
        record(doc_id, name, "QUEUED", file_type=kind, sha256=sha, reason="Waiting for the background worker")
        background.add_task(pipeline.run, doc_id, path)
    return jobs.get_job(job_id)


# ----------------------------------------------------------------------- jobs

@app.get("/api/jobs", response_model=JobPage)
def list_jobs(collection: str | None = Query(default=None),
              page: int = Query(default=0, ge=0, description="0-based page index"),
              size: int = Query(default=settings.LIST_PAGE_SIZE, ge=1, le=settings.LIST_MAX_PAGE_SIZE)):
    """Paginated job listing (newest first): JOB ID, FILES, STATUS, REASON. Defaults: page=0, size=5."""
    return _paginate(jobs.list_jobs(collection), page, size)


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
def job_status(job_id: str):
    """Poll one upload job; includes its documents."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


# ----------------------------------------------------------------------- documents

@app.get("/api/documents", response_model=DocumentPage)
def list_documents(collection: str | None = Query(default=None),
                   job_id: str | None = Query(default=None),
                   status: DocStatus | None = Query(default=None),
                   page: int = Query(default=0, ge=0, description="0-based page index"),
                   size: int = Query(default=settings.LIST_PAGE_SIZE, ge=1, le=settings.LIST_MAX_PAGE_SIZE)):
    """Paginated listing of every uploaded file (newest first): DOC ID, NAME, STATUS, REASON
    (+ duplicate link). Filter by job_id and/or status. Defaults: page=0, size=5."""
    docs = jobs.list_docs(collection, job_id)
    return _paginate([d for d in docs if status is None or d.status == status], page, size)


@app.get("/api/documents/{doc_id}", response_model=DocumentInfo)
def get_document(doc_id: str):
    doc = jobs.get(doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    return doc


_INLINE_TYPES = {"pdf": "application/pdf", "html": "text/html; charset=utf-8", "text": "text/plain; charset=utf-8",
                 "csv": "text/csv; charset=utf-8"}


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str, download: bool = Query(default=False)):
    """The stored original file. PDFs, HTML, text, CSV and images render inline in the browser;
    other types (DOCX, RTF) are served as downloads."""
    doc = jobs.get(doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    path = next(iter(settings.uploads_dir.glob(f"{doc_id}_*")), None)
    if path is None:
        raise HTTPException(410, "the uploaded file is no longer stored")
    if doc.file_type == "image":
        import mimetypes
        media = mimetypes.guess_type(doc.filename)[0] or "application/octet-stream"
    else:
        media = _INLINE_TYPES.get(doc.file_type or "", "application/octet-stream")
    disposition = "attachment" if download or media == "application/octet-stream" else "inline"
    return FileResponse(path, media_type=media, filename=doc.filename if disposition == "attachment" else None,
                        headers={"Content-Disposition": f'{disposition}; filename="{doc.filename}"'})


@app.post("/api/documents/{doc_id}/retry", response_model=DocumentInfo, status_code=202)
def retry_document(doc_id: str, background: BackgroundTasks):
    """Re-queue a FAILED document (the stored file is processed again with the current code)."""
    doc = jobs.get(doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.status != "FAILED":
        raise HTTPException(409, f"only FAILED documents can be retried; this one is {doc.status}")
    path = next(iter(settings.uploads_dir.glob(f"{doc_id}_*")), None)
    if path is None:
        raise HTTPException(410, "the uploaded file is no longer stored; upload it again")
    log.info("[retry] %s", ctx(job=doc.job_id, doc=doc_id, file=doc.filename))
    doc = jobs.update(doc_id, status="QUEUED", stage=None, pages=0, chunks=0, ocr_pages=0,
                      reason="Retry requested; waiting for the background worker")
    background.add_task(pipeline.run, doc_id, path)
    return doc


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str):
    doc = jobs.get(doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.status not in TERMINAL_STATUSES:
        raise HTTPException(409, f"document is {doc.status}; wait until it finishes")
    if doc.status == "COMPLETED":
        vector_store.delete_document(doc.collection, doc_id)
        bm25_index.rebuild(doc.collection)
    for p in settings.uploads_dir.glob(f"{doc_id}_*"):
        p.unlink(missing_ok=True)
    (settings.pages_dir / f"{doc_id}.json").unlink(missing_ok=True)
    jobs.delete(doc_id)
    log.info("[delete] %s", ctx(job=doc.job_id, doc=doc_id, file=doc.filename))


@app.delete("/api/collections/{name}", status_code=204)
def reset_collection(name: str):
    col = _collection(name)
    log.info("[reset] collection=%s", col)
    vector_store.delete_collection(col)
    bm25_index.drop(col)
    for doc in jobs.list_docs(col):
        for p in settings.uploads_dir.glob(f"{doc.doc_id}_*"):
            p.unlink(missing_ok=True)
        (settings.pages_dir / f"{doc.doc_id}.json").unlink(missing_ok=True)
        jobs.delete(doc.doc_id)
    for job in jobs.list_jobs(col):
        jobs.delete_job(job.job_id)


@app.get("/api/chunks", response_model=list[ChunkOut])
def list_chunks(collection: str | None = Query(default=None),
                doc_id: str | None = Query(default=None), job_id: str | None = Query(default=None),
                limit: int = Query(default=50, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
    """Inspect the document_chunks store; filter by document set, document and/or upload job."""
    col = _collection(collection)
    chunks = vector_store.get_all_chunks(col, [doc_id] if doc_id else None, job_id)[offset:offset + limit]
    return [_chunk_out(c, col) for c in chunks]


def _chunk_out(c: dict, col: str) -> ChunkOut:
    m = c["metadata"]
    return ChunkOut(chunk_id=c["chunk_id"], doc_id=m.get("doc_id", ""), job_id=m.get("job_id", ""),
                    collection=m.get("collection", col), source=m.get("source", ""), page=int(m.get("page", 0)),
                    section=m.get("section") or None, chunk_index=int(m.get("chunk_index", 0)),
                    char_start=int(m.get("char_start", 0)), char_end=int(m.get("char_end", 0)),
                    extraction=m.get("extraction", "text"), text=c["text"])


@app.get("/api/documents/{doc_id}/pages", response_model=DocumentPages)
def document_pages(doc_id: str):
    """Page text of a COMPLETED document for the show-page viewer, with the chunk ids per page.

    Uses the page text stored at ingest; documents indexed before that existed are reconstructed
    from chunk offsets (`source: "reconstructed"`)."""
    import json

    doc = jobs.get(doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.status != "COMPLETED":
        raise HTTPException(409, f"document is {doc.status}, not COMPLETED")
    stored = settings.pages_dir / f"{doc_id}.json"
    if stored.exists():
        pages = [PageOut(**pg) for pg in json.loads(stored.read_text())]
        return DocumentPages(doc_id=doc_id, filename=doc.filename, pages=pages, source="stored")
    # Reconstruct: lay every chunk's text at its char_start; overlaps carry identical text.
    by_page: dict[int, list[ChunkOut]] = {}
    for c in vector_store.get_all_chunks(doc.collection, [doc_id]):
        out = _chunk_out(c, doc.collection)
        by_page.setdefault(out.page, []).append(out)
    pages = []
    for page_no in sorted(by_page):
        cs = sorted(by_page[page_no], key=lambda x: x.chunk_index)
        buf: list[str] = []
        for x in cs:
            while len(buf) < x.char_start:
                buf.append(" ")
            buf[x.char_start:x.char_start + len(x.text)] = list(x.text)
        pages.append(PageOut(page=page_no, section=cs[0].section, extraction=cs[0].extraction,
                             text="".join(buf), chunk_ids=[x.chunk_id for x in cs]))
    return DocumentPages(doc_id=doc_id, filename=doc.filename, pages=pages, source="reconstructed")


# ----------------------------------------------------------------------- ask

def _resolve_scope(req: AskRequest) -> tuple[str, list[str] | None, list[str]]:
    """Turn doc_ids / job_ids into the concrete set of COMPLETED documents to search.

    Returns (collection, doc_ids or None for the whole collection, job_ids used).
    Validation errors are explicit so a client can fix its selection:
      404 unknown id · 409 document/job not COMPLETED · 400 mixed collections.
    """
    if not req.doc_ids and not req.job_ids:
        return _collection(req.collection), None, []

    selected: dict[str, DocumentInfo] = {}
    job_ids: list[str] = []
    for jid in req.job_ids or []:
        job = jobs.get_job(jid)
        if job is None:
            raise HTTPException(404, f"job '{jid}' not found")
        job_ids.append(jid)
        completed = [d for d in job.documents if d.status == "COMPLETED"]
        if not completed:
            raise HTTPException(409, f"job '{jid}' has no COMPLETED document yet ({job.status}: {job.reason})")
        for d in completed:
            selected[d.doc_id] = d
    for did in req.doc_ids or []:
        d = jobs.get(did)
        if d is None:
            raise HTTPException(404, f"document '{did}' not found")
        if d.status == "DUPLICATE" and d.duplicate_of:
            original = jobs.get(d.duplicate_of)
            if original is not None:
                d = original  # a duplicate points at the indexed original
        if d.status != "COMPLETED":
            raise HTTPException(409, f"document '{did}' ({d.filename}) is {d.status}, not COMPLETED")
        selected[d.doc_id] = d

    collections = {d.collection for d in selected.values()}
    if len(collections) > 1:
        raise HTTPException(400, f"selected documents span several collections {sorted(collections)}; "
                                 "select documents from one collection")
    col = collections.pop()
    if req.collection and _collection(req.collection) != col:
        raise HTTPException(400, f"collection '{req.collection}' does not match the selected documents ('{col}')")
    return col, sorted(selected), job_ids


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest):
    """Ask over the whole collection, or only over selected documents / jobs.

    The answer text carries [n] markers; `citations` lists exactly the sources those markers
    refer to. Markers with no matching source are removed and reported in `unresolved_citations`.
    """
    col, doc_ids, job_ids = _resolve_scope(req)
    try:
        return answer(req.question, col, req.top_k, doc_ids=doc_ids, job_ids=job_ids)
    except LLMError as exc:
        raise HTTPException(502, str(exc))
