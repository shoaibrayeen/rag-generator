# Architecture

RAG Generator turns an arbitrary set of documents into a grounded question-answering
service at runtime. There are two flows: **ingest** (asynchronous) and **ask** (synchronous).

## Components

| Component | Module | Role |
|---|---|---|
| API | `app/main.py` | FastAPI routes, background job scheduling, static UI + docs |
| Job & document registry | `app/jobs.py` | jobs (`data/jobs.json`) and documents (`data/documents.json`); a job's status is derived from its documents on every read |
| Extractors | `app/ingest/extractors.py` | file → pages with metadata; primary extractor per format + fallback tier (Tesseract OCR) |
| Chunker | `app/ingest/chunker.py` | overlapping windows, boundary-aware, metadata-tagged |
| Pipeline | `app/ingest/pipeline.py` | background worker: extract → chunk → embed → store → BM25 rebuild |
| Embedder | `app/retrieval/embedder.py` | fastembed (`BAAI/bge-small-en-v1.5`, ONNX, CPU) |
| Vector store | `app/retrieval/vector_store.py` | ChromaDB `PersistentClient`; single `document_chunks` collection; document set / document / job are metadata filters |
| Lexical index | `app/retrieval/bm25_index.py` | rank-bm25 `BM25Okapi` per collection, rebuilt from Chroma |
| Fusion | `app/retrieval/fusion.py` | Reciprocal Rank Fusion of dense and BM25 rankings |
| LLM client | `app/llm/client.py` | OpenAI-compatible `chat/completions` over httpx |
| Prompts | `app/llm/prompts.py` | grounded-answer system prompt, `[n]` context formatting |
| Orchestrator | `app/rag.py` | `answer()` = retrieve → prompt → LLM → answer + sources |
| UI | `app/static/index.html` | upload · listing · ask · sources |
| CLI | `cli/rag_cli.py` | `rag ingest/status/job/ask/reset/serve` over HTTP |
| Evaluation | `evaluation/` | Claude-as-judge scoring, dataset generation |
| Config | `app/config.py` | every tunable, overridable from `.env` |

## Ingest flow (asynchronous)

```
client ──POST /api/documents (multipart, collection)──► API creates ONE job (job_id)
                                                        │ per file (each becomes a document of that job):
                                                        │  read bytes ──fail──► UPLOAD_FAILED
                                                        │  extension ∉ SUPPORTED_EXTENSIONS ──► EXTRACTION_NOT_SUPPORTED
                                                        │  0 bytes ──► EMPTY_FILE
                                                        │  > MAX_UPLOAD_MB ──► UPLOAD_FAILED
                                                        │  sha256 already live in collection ──► DUPLICATE (links original)
                                                        │  store to data/uploads/<doc_id>_<name> ──fail──► UPLOAD_FAILED
                                                        │  else ──► QUEUED + background task
                                                        ▼
client ◄── 202 {job_id, status, reason, counts, documents:[{doc_id, status, reason, …}]} ──┘

background worker (FastAPI BackgroundTasks, thread pool)
   QUEUED ─► PROCESSING/extracting ─► PROCESSING/chunking ─► PROCESSING/embedding ─► COMPLETED
                      │                        │                       │
                      └──────── any exception ─┴───────────────────────┴──► FAILED ("PROCESSING FAILED: …")

client ──GET /api/jobs──► job listing (JOB ID · FILES · STATUS · REASON)
client ──GET /api/jobs/{job_id}──► job + its documents
client ──GET /api/documents?job_id=──► documents of one job (DOC ID · NAME · STATUS · REASON)
```

Job status is never stored; it is derived from the documents each time it is read:
`PROCESSING` if any document is processing (or some are queued and others done), `QUEUED` if
all are queued, `COMPLETED` if all are terminal and at least one is `COMPLETED` or `DUPLICATE`
(reason prefixed "partially completed" when others failed), otherwise `FAILED`.

Every status is persisted after each transition, so a listing is always consistent with
what the worker has done. Jobs that were mid-flight during a restart are marked `FAILED`
on load with an explanatory reason.

### Extraction: primary tier, then fallback tier

`extract()` runs the format's **primary** extractor. If it raises or returns no pages, the
format's **fallback** runs. If that also yields nothing, an `ExtractionError` with a precise,
user-facing reason becomes the job's `FAILED` reason.

| Format | Primary | Fallback (only when primary fails / finds no text) | "page" means | Section metadata |
|---|---|---|---|---|
| PDF | PyMuPDF `get_text` per page; a page with < `OCR_MIN_CHARS_PER_PAGE` chars is OCR'd in place | render every page with PyMuPDF → Tesseract | printed page | – |
| DOCX | python-docx paragraphs + tables | OCR of embedded images (`word/media/*`) → Tesseract | one page per heading block | heading text |
| HTML | beautifulsoup4 + lxml | stdlib `html.parser` tag stripping | 1 | `<title>` |
| MD | stdlib (multi-encoding decode) | – | one page per heading | heading text |
| TXT | stdlib (multi-encoding decode) | – | 1 | – |
| CSV | stdlib csv | – | `CSV_ROWS_PER_PAGE` rows (header repeated) | `rows a-b` |
| PNG/JPG/TIFF | Tesseract (there is no text layer) | – | one page per frame | – |

Tesseract is therefore never the first choice for a document that has a text layer; it is
the safety net for scanned, image-only or otherwise unreadable content.

Each chunk records: `doc_id, source, file_type, collection, page, section, extraction
(text|ocr), chunk_index, char_start, char_end`.

### Chunking

Sliding window of `CHUNK_SIZE_CHARS` (default 1200) with `CHUNK_OVERLAP_CHARS` (200) of
overlap. The cut point is chosen in the last quarter of the window at the first available
boundary in this priority: blank line, newline, sentence end, clause, word.

## Ask flow (synchronous)

```
{question, collection | doc_ids | job_ids}
   └─► resolve scope: job_ids → their COMPLETED documents; duplicate ids → their original;
       validate (404 unknown, 409 not COMPLETED, 400 mixed collections) → concrete doc_id set or "whole collection"
question ─► embed (fastembed) ─► Chroma cosine top DENSE_TOP_K (where doc_id ∈ scope) ──┐
        └─► tokenize          ─► BM25 top BM25_TOP_K (chunks filtered to scope) ─┴─► RRF: score = Σ 1/(RRF_K + rank)
                                                                         │ keep FUSED_TOP_K
                                                                         ▼
            system prompt (answer only from context, cite [n], say "not found" otherwise)
          + user prompt: "[1] (source, page N, section) text …" × k + question
                                                                         │
                                                                         ▼
                     POST {LLM_API_URL}/chat/completions (Bearer LLM_API_KEY, model LLM_MODEL)
                                                                         │
                                                                         ▼
            answer text ─► citation check: every [n] must match a retrieved source; unmatched markers are
                           removed from the text and listed in unresolved_citations
                                                                         ▼
 {answer, citations:[sources actually cited, first-use order], sources:[all retrieved], unresolved_citations, scope, latency_ms}
```

Reciprocal Rank Fusion is used instead of score normalisation because dense cosine
distances and BM25 scores live on incomparable scales; RRF only needs ranks and rewards
chunks that both retrievers agree on.

## Persistence and process model

Three named stores (names are settings):

| Store | Backing | Contents |
|---|---|---|
| `jobs` | `data/jobs.json` | `{job_id, collection, doc_ids, created_at}`; status derived on read |
| `documents` | `data/documents.json` | one record per uploaded file: status, reason, stage, sha256, job_id, duplicate_of, counts |
| `document_chunks` | ChromaDB collection in `data/chroma/` | chunk text, embedding and metadata `{collection, doc_id, job_id, source, file_type, page, section, extraction, chunk_index, char_start, char_end}` |

Retrieval scopes are metadata `where` clauses on `document_chunks`
(`{"collection": …}` and optionally `{"doc_id": {"$in": […]}}`); the BM25 index is rebuilt per
document set from the same store. `data/uploads/` keeps the original files. Everything sits
under one Docker volume.
- BM25 is in-memory and derived from Chroma; it is rebuilt on startup for every collection
  and after each successful ingest or delete. This keeps a single source of truth and makes
  restarts safe, at the cost of requiring a single app process.
- The LLM and embedding models are the only external dependencies; the LLM is reached
  purely via `.env` settings.

## Evaluation

`evaluation/run_eval.py` replays a JSONL dataset through `/api/ask`, then asks Claude
(`JUDGE_MODEL`, official `anthropic` SDK, structured outputs) for faithfulness, answer
relevance, context relevance, correctness and a hallucination flag. `generate_dataset.py`
synthesises questions from indexed chunks so the eval works for any new document set.
