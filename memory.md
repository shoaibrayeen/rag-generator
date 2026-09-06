# Project memory

Facts, decisions and gotchas that are not obvious from the code. Update this whenever a
decision is made or a surprise is discovered (see `CLAUDE.md`).

## Purpose
Agentic-coding assessment deliverable: a RAG Generator that accepts documents at runtime,
indexes them, and answers grounded questions, with no code changes between document sets.
Deliverables also include complete agent transcripts (`transcripts/`) and a Claude-as-judge
evaluation script.

## Decisions (with reasons)
- **FastAPI over Flask** — async upload endpoint, background tasks, free OpenAPI docs.
- **OpenAI-compatible generation endpoint** — the brief requires that only `LLM_API_URL`
  and `LLM_API_KEY` (plus `LLM_MODEL`) change per deployment; this protocol is served by
  OpenAI, Azure, OpenRouter, Groq, vLLM, Ollama, LiteLLM.
- **Judge = Claude via the official `anthropic` SDK** with its own `ANTHROPIC_API_KEY`
  (`JUDGE_MODEL=claude-opus-5`, `messages.parse` structured outputs). Independent from the
  generation model so the judge stays Claude even when generation is not.
- **ChromaDB embedded** (`PersistentClient`) rather than a separate server — one compose service.
- **Three named stores** (user requirement): `jobs` + `documents` JSON registries, and ONE Chroma
  collection `document_chunks` with metadata `collection` / `doc_id` / `job_id`. Document sets are a
  metadata filter, not separate Chroma collections (earlier versions used one Chroma collection per
  set; old `data/chroma` dirs from before v0.4.0 must be deleted).
- **BM25 in memory, rebuilt from Chroma** — single source of truth; requires one app process.
- **RRF (k=60)** for fusion — dense distances and BM25 scores are on incomparable scales.
- **fastembed only embeds**; chunking is our own boundary-aware sliding window.
- **Duplicate detection is per collection** by SHA-256 of the bytes; the same file in another
  collection is a legitimate separate document set.
- **Rejected uploads are records, not errors** — the listing must explain every request
  (DOC ID · NAME · STATUS · REASON), per the brief.
- **Two-tier extraction; Tesseract is strictly the fallback** (user requirement 2026-09-06): primary =
  PyMuPDF / python-docx / beautifulsoup4 / stdlib; fallback runs only when the primary raises or yields
  no text (OCR of PDF pages, OCR of DOCX-embedded images, stdlib HTML stripping). Single scanned pages
  inside a digital PDF are OCR'd in place. `ExtractionError` carries the user-facing FAILED reason.
- **PyMuPDF replaced pypdf + pypdfium2** — one library for text and page rendering.
- **Server-side refusal-fallback beta not used on the judge** — benign prompts, simpler script.

- **Content sniffing beats extensions** (2026-09-06 incident: `2P CRA dated 17_12_2025.docx` was RTF;
  python-docx raised PackageNotFoundError and the fallback message blamed missing OCR). `sniff_type()`
  now routes by magic bytes; RTF is supported; legacy OLE `.doc` gets a "convert to .docx" reason.
- **Upload order is fixed by the user**: validate → store file + compute SHA-256 → duplicate check → QUEUED.
- **Failure reasons must name the primary reader's error** (paths stripped) before the fallback's text.
- **Every step logs `[step] … job= doc=`** via `app/logging_utils.py`; `grep doc=<id>` tells the story of a file.

- UI pages: `/` workbench keeps 5-per-page panels (user asked to keep it); `/jobs` and `/documents` are 20-per-page;
  show page is `/documents/{doc_id}` (static HTML reads the id from the path).

## Environment gotchas
- The dev Mac has no Docker, Homebrew or Tesseract; Python is 3.9 system-wide. Use
  `uv venv --python 3.11 .venv`. Docker image must be built/verified elsewhere.
- Locally OCR is therefore unavailable: `scanned_memo.pdf` and `whiteboard.png` end `FAILED` with
  "…Tesseract OCR is not installed on this host…" — expected; they succeed inside Docker.
- `scripts/mock_llm.py` (port 8001) is an extractive stand-in for demos/CI. It is not a model.
- First embedding-model load downloads ~130 MB; the Dockerfile pre-downloads it.
- fastembed + chromadb pin numpy/onnxruntime jointly; regenerate `requirements.txt` with
  `uv pip compile requirements.in -o requirements.txt`.

## Conventions
- All tunables in `app/config.py`; `.env.example` mirrors every field.
- A **job** is one upload request; its documents carry `job_id`. Job status is *derived* from its
  documents on every read (never stored). Document terminal statuses: everything except `QUEUED`
  and `PROCESSING`; job terminal statuses: `COMPLETED`, `FAILED`.
- Scoped ask: `doc_ids`/`job_ids` → concrete COMPLETED doc set in one collection; Chroma `where`
  `{doc_id: {$in: [...]}}` + BM25 candidate filter. Citations are validated in `app/rag.py`
  (`extract_citations`); bogus `[n]` markers are stripped and reported.
- Listings are paginated: 0-based `page`, default `size` 5 (`LIST_PAGE_SIZE`), envelope with
  `items/total/pages/has_next/has_prev`. Clients must read `.items`.
- Collection names follow Chroma's rule (3-63 chars, alnum at both ends) — 2-char names fail in Chroma.
- Tests mock `app.rag.llm.chat`; no network needed.
- Version lives in `app/main.py` (`FastAPI(version=…)`) and `documentation/changelog.html`.
- Docs live in `documentation/` (the user asked for this name, not `docs/`; `/docs` stays Swagger).
  Every `.md` there and `README.md` gets an `.html` twin via `scripts/build_docs.py`.

## History
- 2026-09-06 — v0.1.0 initial build; v0.2.0 async job model with fixed statuses, dedupe, docs set;
  v0.3.0 PyMuPDF primary + Tesseract-as-fallback tiering, transcript/rules/docs scripts;
  v0.4.0 jobs as first-class objects, job→document filter, scoped ask, validated citations, `plans/`;
  v0.5.0 paginated listings (page 0, size 5), `documentation/enhancements.md`;
  v0.6.0 content sniffing + RTF, precise failure reasons, retry endpoint, store-then-hash upload order, step logging;
  v0.7.0 `/jobs`, `/documents` (20/page) and `/documents/{id}` show page (+ `/api/documents/{id}/file`).
