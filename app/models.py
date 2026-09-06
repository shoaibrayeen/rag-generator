"""Pydantic schemas shared by the API, CLI and evaluation script."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# The complete, fixed set of job statuses. `reason` explains every non-QUEUED state.
DocStatus = Literal[
    "QUEUED",                    # accepted, waiting for the background worker
    "DUPLICATE",                 # same file hash already in this collection; not processed
    "PROCESSING",                # extracting / chunking / embedding (see `stage`)
    "COMPLETED",                 # indexed and searchable
    "FAILED",                    # processing failed; see reason
    "EMPTY_FILE",                # zero-byte upload
    "EXTRACTION_NOT_SUPPORTED",  # file extension not in SUPPORTED_EXTENSIONS
    "UPLOAD_FAILED",             # could not read or store the upload; see reason
]
TERMINAL_STATUSES = {"DUPLICATE", "COMPLETED", "FAILED", "EMPTY_FILE",
                     "EXTRACTION_NOT_SUPPORTED", "UPLOAD_FAILED"}


JobStatus = Literal["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]


class DocumentInfo(BaseModel):
    job_id: str                      # the upload request this document belongs to
    doc_id: str
    filename: str
    file_type: Optional[str] = None
    collection: str
    status: DocStatus
    reason: Optional[str] = None     # human-readable explanation of the current status
    stage: Optional[str] = None      # PROCESSING sub-step: extracting | chunking | embedding
    sha256: Optional[str] = None
    duplicate_of: Optional[str] = None       # doc_id of the original when status == DUPLICATE
    duplicate_of_name: Optional[str] = None
    pages: int = 0
    chunks: int = 0
    ocr_pages: int = 0
    created_at: str
    updated_at: str


class JobInfo(BaseModel):
    """One upload request. Status is derived from its documents every time it is read."""
    job_id: str
    collection: str
    status: JobStatus
    reason: str
    files: int
    counts: dict[str, int]           # documents per status, e.g. {"COMPLETED": 2, "DUPLICATE": 1}
    doc_ids: list[str]
    created_at: str
    updated_at: str


class JobDetail(JobInfo):
    documents: list[DocumentInfo]


UploadResponse = JobDetail


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    collection: Optional[str] = None
    doc_ids: Optional[list[str]] = Field(default=None, description="restrict to these documents")
    job_ids: Optional[list[str]] = Field(default=None, description="restrict to the documents of these jobs")
    top_k: Optional[int] = Field(default=None, ge=1, le=50)


class AskScope(BaseModel):
    collection: str
    doc_ids: list[str]               # documents actually searched ([] = whole collection)
    job_ids: list[str]
    restricted: bool


class Source(BaseModel):
    n: int
    chunk_id: str
    doc_id: str
    source: str
    file_type: str
    page: int
    section: Optional[str] = None
    extraction: str = "text"
    text: str
    dense_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    rrf_score: float


class AskResponse(BaseModel):
    question: str
    answer: str                      # answer text with [n] citation markers
    citations: list[Source]          # the sources actually cited in the answer, in order of first use
    sources: list[Source]            # everything retrieved (superset of citations)
    unresolved_citations: list[int] = Field(default_factory=list)  # [n] markers the LLM produced that match no source (removed from text)
    scope: AskScope
    collection: str
    model: str
    latency_ms: int


class ChunkOut(BaseModel):
    chunk_id: str
    doc_id: str
    source: str
    page: int
    section: Optional[str] = None
    text: str


class HealthResponse(BaseModel):
    status: str
    llm_model: str
    llm_api_url: str
    embedding_model: str
    supported_extensions: list[str]
    ocr_enabled: bool
    ocr_available: bool
    collections: list[str]
    statuses: list[str]
    settings: dict
