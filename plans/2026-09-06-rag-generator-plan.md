# RAG Generator — Implementation Plan

## Context

Agentic-coding assessment: build a **RAG Generator** that accepts documents at runtime, indexes them, and answers questions grounded in those documents, with no code changes needed for a different document set. Deliverables also include AI-agent transcripts (this session, exported to `transcripts/`) and an evaluation script that uses Claude as judge.

The repo is empty (placeholder README only), so this is a greenfield build.

**Decisions confirmed with the user:**
- Web framework: **FastAPI** (serves the single HTML page + JSON API)
- Generation LLM: **OpenAI-compatible** `chat/completions` endpoint, configured only via `.env` (`LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL`)
- Judge: **Claude via the `anthropic` SDK** with its own `ANTHROPIC_API_KEY`
- Vector store: **ChromaDB embedded** (`PersistentClient`) inside the app container, data on a volume
- Embeddings: **fastembed** (`BAAI/bge-small-en-v1.5`); chunking is a custom overlapping, metadata-tagged splitter (fastembed only embeds, it does not chunk)
- Lexical retrieval: **rank-bm25**; dense + BM25 results merged with **Reciprocal Rank Fusion**
- Deployment: **Docker + docker compose**
- Formats: PDF, DOCX, HTML, TXT, MD, CSV → text with page metadata, driven by a user-editable constant

## Tech stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.11 |
| API | FastAPI + uvicorn |
| UI | Single static `index.html` (vanilla JS, no build step) |
| CLI | `typer` app talking to the running API over HTTP (`httpx`) so there is exactly one Chroma writer |
| Extraction | `pypdf` (PDF), `python-docx` (DOCX), `beautifulsoup4` (HTML), stdlib for TXT/MD/CSV |
| OCR fallback | Tesseract via `pytesseract` + `pypdfium2` page rendering; kicks in only when a PDF page has no extractable text, and for image inputs |
| Chunking | custom sliding window, char-based, sentence/paragraph-boundary aware |
| Embeddings | `fastembed` `TextEmbedding` |
| Vector DB | `chromadb` `PersistentClient`, cosine space |
| Lexical | `rank-bm25` `BM25Okapi`, rebuilt from Chroma on startup / after ingest |
| Fusion | Reciprocal Rank Fusion (k=60) |
| Generation LLM | OpenAI-compatible `POST {LLM_API_URL}/chat/completions` via `httpx` |
| Judge | `anthropic` SDK, `claude-opus-5`, structured output via `client.messages.parse(..., output_format=PydanticModel)` |
| Config | `pydantic-settings` reading `.env` |
| Tests | `pytest` (chunker, fusion, extractors, API smoke) |
| Not used (state in README) | LangChain / LlamaIndex, rerankers, external Chroma server, auth, GPU |

## Repository layout

```
rag-generator/
├── app/
│   ├── main.py            # FastAPI app, routes, static mount, lifespan (load index)
│   ├── config.py          # Settings (from .env) + editable CONSTANTS (see below)
│   ├── models.py          # pydantic request/response schemas
│   ├── ingest/
│   │   ├── extractors.py  # per-format -> list[Page(text, page_no, meta)]
│   │   ├── chunker.py     # overlapping, metadata-tagged chunks
│   │   └── pipeline.py    # ingest job: extract -> chunk -> embed -> store, status updates
│   ├── retrieval/
│   │   ├── embedder.py    # fastembed wrapper (singleton)
│   │   ├── vector_store.py# Chroma wrapper: add/query/delete/list, per-collection
│   │   ├── bm25_index.py  # rank-bm25 wrapper, tokenizer, rebuild from Chroma
│   │   └── fusion.py      # RRF + hybrid retrieve()
│   ├── llm/
│   │   ├── client.py      # OpenAI-compatible chat client (httpx)
│   │   └── prompts.py     # grounded-answer system prompt, context formatting
│   ├── rag.py             # answer(question, collection) -> answer + sources
│   ├── jobs.py            # in-memory job registry + persisted documents.json
│   └── static/index.html  # the UI
├── cli/rag_cli.py         # typer CLI: ingest | status | ask | reset | serve
├── evaluation/
│   ├── run_eval.py        # runs dataset through /api/ask, judges, writes report
│   ├── judge.py           # Claude judge (anthropic SDK, structured outputs)
│   ├── generate_dataset.py# optional: synthesize Q/A pairs from indexed chunks
│   ├── dataset.example.jsonl
│   └── reports/           # gitignored outputs
├── samples/               # small md/txt/csv/html (+ script to make pdf/docx) for the demo
├── scripts/make_samples.py
├── tests/
├── transcripts/README.md  # where /export transcripts go
├── data/                  # runtime: chroma/, uploads/, documents.json (gitignored, volume)
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── requirements.txt
├── pyproject.toml         # console script `rag` -> cli
└── README.md
```

## Configuration

### `.env` (the only file the user must edit to go live)
Minimum required: `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` (plus `ANTHROPIC_API_KEY` to run the eval). Every other setting below has a default and is optional to override.

### `app/config.py` — the single home for every tunable (nothing tunable is hard-coded elsewhere)
One `Settings` class (`pydantic-settings`) with sensible defaults; every field can be overridden by an env var of the same name in `.env`. Modules import `settings` and never define their own numbers or model names.

```python
class Settings(BaseSettings):
    # --- LLM (generation) ---
    LLM_API_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_S: int = 60
    # --- Embeddings ---
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 64
    # --- Chunking ---
    CHUNK_SIZE_CHARS: int = 1200
    CHUNK_OVERLAP_CHARS: int = 200
    CSV_ROWS_PER_PAGE: int = 50
    # --- OCR fallback (Tesseract) ---
    OCR_ENABLED: bool = True
    OCR_MIN_CHARS_PER_PAGE: int = 20      # below this, a PDF page is treated as scanned and OCR'd
    OCR_LANG: str = "eng"
    OCR_DPI: int = 200
    # --- Retrieval ---
    DENSE_TOP_K: int = 20
    BM25_TOP_K: int = 20
    FUSED_TOP_K: int = 6
    RRF_K: int = 60
    # --- Storage / server ---
    DATA_DIR: Path = Path("data")
    DEFAULT_COLLECTION: str = "default"
    MAX_UPLOAD_MB: int = 50
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # --- Evaluation ---
    ANTHROPIC_API_KEY: str = ""
    JUDGE_MODEL: str = "claude-opus-5"
    JUDGE_MAX_TOKENS: int = 4096
    EVAL_MIN_FAITHFULNESS: float = 3.5
    RAG_API_URL: str = "http://localhost:8000"

SUPPORTED_EXTENSIONS: dict[str, str] = {   # extension -> extractor name; edit to add/remove formats
    ".pdf": "pdf", ".docx": "docx", ".html": "html", ".htm": "html",
    ".txt": "text", ".md": "text", ".csv": "csv",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".tiff": "image",   # via Tesseract OCR
}
```
`.env.example` lists every field above with its default so the user sees the full knob set in one place. The README's "Tuning" section points at this file.

### `requirements.txt` (pinned)
```
fastapi
uvicorn[standard]
python-multipart
pydantic-settings
httpx
typer
fastembed
chromadb
rank-bm25
pypdf
python-docx
beautifulsoup4
lxml
pypdfium2        # renders PDF pages to images for OCR, no poppler needed
pytesseract      # Python binding; needs the tesseract binary (installed in Dockerfile)
pillow
anthropic
pytest
```
Exact versions pinned at implementation time to what `pip` resolves on Python 3.11 (fastembed and chromadb have onnxruntime/numpy constraints that must be resolved together). `reportlab` goes in a separate `requirements-dev.txt` only for `scripts/make_samples.py`.

## Pipeline design

### Ingest (`app/ingest/`)
1. `POST /api/documents` saves each file under `data/uploads/<doc_id>_<name>`, registers a job (`queued`), and schedules `pipeline.run(doc_id)` via FastAPI `BackgroundTasks`.
2. `extractors.extract(path) -> list[Page]` dispatches on `SUPPORTED_EXTENSIONS`:
   - PDF: `pypdf` per page → `page_no` = real page number. **OCR fallback:** if a page yields fewer than `OCR_MIN_CHARS_PER_PAGE` characters (scanned / image-only page) and `OCR_ENABLED`, render that page with `pypdfium2` at `OCR_DPI` and run `pytesseract.image_to_string(lang=OCR_LANG)`. Chunk metadata gets `extraction="ocr"` so sources show it came from OCR.
   - Images (`.png .jpg .jpeg .tiff`): whole file through Tesseract, `page_no=1` (multi-page TIFF → one page per frame).
   - OCR degrades gracefully: if the `tesseract` binary is missing, log a warning once, mark the page `extraction="ocr_unavailable"`, and continue with whatever text pypdf found. Never fails the ingest job.
   - DOCX: paragraphs grouped under headings; `page_no` = sequential section index, `section` = heading text (DOCX has no real pages; state this in README)
   - HTML: `BeautifulSoup`, drop `script/style/nav`, `page_no=1`, `title` metadata
   - TXT/MD: single page, split on blank lines; MD keeps `#` heading as `section`
   - CSV: header row prepended to each block of `CSV_ROWS_PER_PAGE` rows, block index = `page_no`
3. `chunker.chunk(pages, doc_meta)` produces overlapping windows (`CHUNK_SIZE_CHARS`, `CHUNK_OVERLAP_CHARS`), preferring to cut at paragraph/sentence boundaries. Each chunk carries metadata: `doc_id, source (filename), file_type, page, section, chunk_index, char_start, char_end, collection`. Chunk id = `f"{doc_id}:{chunk_index}"`.
4. `embedder.embed(texts)` → fastembed vectors (batched).
5. `vector_store.add(collection, ids, texts, embeddings, metadatas)`; then `bm25_index.rebuild(collection)` pulls all chunk texts for that collection from Chroma and rebuilds `BM25Okapi`.
6. Status transitions persisted to `data/documents.json`: `queued → extracting → chunking → embedding → indexed | failed(error)`, plus `pages`, `chunks`, timestamps.

### Query (`app/rag.py`, `app/retrieval/fusion.py`)
1. Dense: embed question → Chroma `query(n_results=DENSE_TOP_K)` (cosine).
2. Lexical: tokenize question → BM25 `get_scores` → top `BM25_TOP_K`.
3. Fusion: RRF `score = Σ 1/(RRF_K + rank)` across both lists, keep `FUSED_TOP_K`; record which retriever(s) surfaced each chunk.
4. Prompt: system message instructs grounded answering with `[n]` citations and an explicit "not found in the provided documents" fallback; user message = numbered context blocks (`[n] source, page`) + question.
5. `llm.client.chat(messages)` → OpenAI-compatible request; surface HTTP/auth errors as 502 with a readable message so a wrong `.env` is obvious.
6. Response: `{answer, sources:[{n, chunk_id, source, page, section, text, dense_rank, bm25_rank, rrf_score}], model, latency_ms}`.

### API surface (`app/main.py`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | UI |
| GET | `/api/health` | liveness + LLM/embedding config (no secrets) |
| POST | `/api/documents` | multipart upload (multi-file), optional `collection` |
| GET | `/api/documents?collection=` | list docs with status/chunk counts |
| GET | `/api/documents/{doc_id}` | status detail |
| DELETE | `/api/documents/{doc_id}` | remove file, chunks, rebuild BM25 |
| GET | `/api/chunks?collection=&limit=` | sample chunks (used by eval dataset generator + "view sources") |
| POST | `/api/ask` | `{question, collection?, top_k?}` → answer + sources |
| DELETE | `/api/collections/{name}` | reset a collection (switch document sets without code changes) |

Startup lifespan: open Chroma, warm the fastembed model, rebuild BM25 for every existing collection so restarts keep working.

### UI (`app/static/index.html`)
One page, four panels: **Upload** (drag-drop / file picker, collection name, shows accepted extensions from `/api/health`), **Status** (table polling `/api/documents` every 2s while any job is non-terminal), **Ask** (question box, answer with `[n]` citations), **Sources** (expandable cards per cited chunk: file, page/section, retriever ranks, chunk text). Plain fetch calls, no framework.

### CLI (`cli/rag_cli.py`, console script `rag`)
`rag ingest <files...> [--collection]`, `rag status [--watch]`, `rag ask "<q>" [--collection] [--show-sources]`, `rag reset [--collection]`, `rag serve` (runs uvicorn locally). All except `serve` call the API at `RAG_API_URL`.

## Evaluation (`evaluation/`)
- **Dataset**: JSONL rows `{question, reference_answer?, collection?}`. Ship `dataset.example.jsonl` matching `samples/`.
- **`generate_dataset.py`** (optional): pulls chunks from `/api/chunks`, asks Claude to write N question/reference pairs per chunk → JSONL. Lets the eval work on any new document set.
- **`run_eval.py`**: for each row, `POST /api/ask`, then judge. Judge model `claude-opus-5` via `client.messages.parse` with a pydantic `JudgeVerdict` (`faithfulness 1-5`, `answer_relevance 1-5`, `context_relevance 1-5`, `correctness 1-5 | null` when no reference, `hallucinated: bool`, `rationale`). Prompt gives question, retrieved contexts, answer, optional reference. Thinking left at model default (adaptive). Writes `reports/<ts>.json` + `reports/<ts>.md` with per-question rows and mean scores, exits non-zero if mean faithfulness < threshold (`--min-faithfulness`, default 3.5).
- Judge deliberately does **not** use the server-side refusal-fallback beta; judge prompts are benign and it keeps the script to one plain `parse` call. Noted in README.

## Docker
- `Dockerfile`: `python:3.11-slim`, `apt-get install tesseract-ocr tesseract-ocr-eng` (OCR fallback), install `requirements.txt`, **pre-download the fastembed model at build time** (`FASTEMBED_CACHE_PATH=/models`) so first run is offline-safe, copy app, `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: single `app` service, `env_file: .env`, `ports: 8000:8000`, volume `./data:/app/data`, healthcheck on `/api/health`.
- Eval runs from host (`python -m evaluation.run_eval`) or `docker compose run --rm app python -m evaluation.run_eval`.

## README contents
Setup (copy `.env.example` → `.env`, set `LLM_API_URL` + `LLM_API_KEY` + `LLM_MODEL`, `docker compose up --build`), local dev without Docker, UI walkthrough, CLI usage, API table, **how to change supported formats / chunk sizes / top-k** (points at the constants), architecture diagram (ingest + hybrid retrieval + fusion), tech stack used vs. not used and why, evaluation instructions, transcripts folder note, known limitations (DOCX pages are sections, single-process BM25 in memory, no auth, OCR is English-only by default and needs the `tesseract` binary when running outside Docker: `brew install tesseract` / `apt install tesseract-ocr`).

## Transcripts
`transcripts/README.md` explaining that Claude Code session transcripts go here via `/export`. The `/export` step itself must be run by the user in the Claude Code terminal at the end of the session; I will remind them and name the target path `transcripts/<date>-rag-generator-build.md`.

## Implementation order
1. Scaffold: `requirements.txt`, `pyproject.toml`, `.env.example`, `.gitignore`, `config.py`, `models.py`
2. Extractors + chunker + unit tests
3. Embedder, vector store, BM25, fusion + unit tests
4. LLM client + prompts + `rag.py`
5. Jobs + ingest pipeline + FastAPI routes
6. `index.html`
7. CLI
8. Samples + `scripts/make_samples.py`
9. Evaluation (judge, run_eval, generate_dataset, example dataset)
10. Dockerfile, compose, README, transcripts folder
11. Build, run, preview, fix

## Verification
1. `pytest` green (chunk overlap/metadata, RRF ordering, each extractor on a sample file, `/api/health`).
2. `docker compose up --build` starts; `/api/health` returns 200 and reports the configured model.
3. **Preview in the Browser pane** at `http://localhost:8000`: upload `samples/*` (all six formats plus a scanned-style PDF and a PNG made by `make_samples.py` to exercise the OCR path; confirm their source cards show `extraction: ocr`), watch status reach `indexed`, ask 2–3 questions, confirm citations and source cards show file + page; ask an off-topic question and confirm the "not in documents" fallback.
4. CLI round-trip: `rag ingest samples/*.md`, `rag status`, `rag ask "..." --show-sources`.
5. Reset flow: `rag reset`, upload a different document set, ask again with no code change.
6. `python -m evaluation.run_eval --dataset evaluation/dataset.example.jsonl` produces a report with scores.
7. Restart the container and confirm the index and BM25 survive (volume + startup rebuild).

---

## Plan changes during implementation (requirements added mid-session, 2026-09-06)

The plan above was approved before coding. The user added requirements while work was in
progress; each was folded into the build and the documentation. Recorded here so the plan
matches what was shipped.

| # | Change | Where it landed |
|---|---|---|
| 1 | Async job model with a **fixed status vocabulary**: `QUEUED, DUPLICATE, PROCESSING, COMPLETED, FAILED, EMPTY_FILE, EXTRACTION_NOT_SUPPORTED, UPLOAD_FAILED`; every request listed as DOC ID · NAME · STATUS · REASON; SHA-256 duplicate detection linking to the original | `app/models.py`, `app/jobs.py`, `app/main.py`, UI listing, CLI `status` |
| 2 | Documentation set: `changelog.html`, `architecture.md`, interactive `flow.html`; all in **`documentation/`** (not `docs/`); every `.md` has an `.html` twin via `scripts/build_docs.py` | `documentation/`, `scripts/build_docs.py` |
| 3 | Agent rules: always update docs, run compile + tests after each change, **never commit**, read `memory.md` when planning; rules mirrored to Cursor (`.cursor/rules/project.mdc`) and Codex (`AGENTS.md`) by `scripts/sync_rules.py` | `CLAUDE.md`, `scripts/sync_rules.py` |
| 4 | `memory.md` project memory; transcripts exported into `transcripts/` (`scripts/export_transcript.py` produces the `/export`-equivalent Markdown) | `memory.md`, `transcripts/`, `scripts/export_transcript.py` |
| 5 | Default port 8000, user-changeable via `APP_PORT` | `app/config.py`, `docker-compose.yml`, README |
| 6 | **Tesseract strictly as fallback**; **PyMuPDF** replaces pypdf/pypdfium2 as the primary PDF extractor; fallback tier for PDF (OCR all pages), DOCX (OCR embedded images), HTML (stdlib stripping); `ExtractionError` gives precise FAILED reasons | `app/ingest/extractors.py`, `app/ingest/pipeline.py` |
| 7 | **Jobs as first-class objects**: one upload request = one job with derived status (`QUEUED/PROCESSING/COMPLETED/FAILED`), `GET /api/jobs`, job → documents filter, and **scoped asking** by selected documents and/or jobs with validated `citations` in the answer | `app/jobs.py`, `app/main.py`, `app/rag.py`, `app/retrieval/*`, UI, CLI `jobs`/`job`/`ask --doc/--job` |
| 8 | This plan committed inside the repo under `plans/` | `plans/` |

Verification performed locally (no Docker/Tesseract on the dev machine): `pytest` suite,
`rag ingest` of the full sample corpus (duplicate, unsupported, empty and OCR-only cases
included), UI upload/status/ask/sources flow in the browser, mock LLM. Docker build to be
verified on a machine with Docker.
