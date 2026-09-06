# RAG Generator

**What it does:** you upload documents while the service is running; it extracts their text
(with page metadata), splits it into overlapping, metadata-tagged chunks, embeds them, and
indexes them for **hybrid retrieval** (vector search + BM25, fused with Reciprocal Rank
Fusion). You then ask questions in a web UI, CLI or REST call and get an answer **grounded in
your documents with `[n]` citations** to the exact file, page and passage. Swap the documents
for a completely different set and it works unchanged — no code edits, only `.env`.

| | |
|---|---|
| Interface | REST API (FastAPI) · single-page UI (upload · requests & status · ask · sources) · `rag` CLI |
| Ingestion | asynchronous jobs: submit → `job_id` → poll status; fixed status vocabulary with reasons; SHA-256 duplicate detection |
| Formats | PDF, DOCX, HTML, TXT, MD, CSV → text with page metadata · PNG/JPG/TIFF and scanned PDF pages via Tesseract OCR |
| Retrieval | dense top-k (fastembed → ChromaDB) **+** BM25 top-k (rank-bm25) → **Reciprocal Rank Fusion** |
| Generation | any OpenAI-compatible LLM endpoint, configured only in `.env` |
| Evaluation | own script with **Claude as judge** |
| Deployment | Docker + docker compose |
| Docs | [`documentation/`](documentation/) — [architecture](documentation/architecture.md) ([html](documentation/architecture.html)) · [interactive flow walkthrough](documentation/flow.html) · [changelog](documentation/changelog.html) · this README as [html](documentation/readme.html) |

---

## 1. Setup

### Docker (recommended)

```bash
cp .env.example .env
# edit .env — set the three required lines:
#   LLM_API_URL=https://api.openai.com/v1      (base URL; /chat/completions is appended)
#   LLM_API_KEY=sk-...
#   LLM_MODEL=gpt-4o-mini
# optional: ANTHROPIC_API_KEY=sk-ant-...        (only for the evaluation script)
docker compose up --build
```

Open <http://localhost:8000> (change the port with `APP_PORT=9000` in `.env`). Interactive API docs: <http://localhost:8000/docs>.
Documentation pages are served at <http://localhost:8000/documentation/flow.html>,
`/documentation/architecture.html`, `/documentation/changelog.html`.

Uploads, the Chroma index and the job registry live in `./data` (a compose volume) and survive restarts.

| Provider | `LLM_API_URL` | `LLM_MODEL` example |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| Ollama (from Docker) | `http://host.docker.internal:11434/v1` | `llama3.1` |
| Azure OpenAI / vLLM / LiteLLM | their OpenAI-compatible base URL | deployment / served model name |

### Local (no Docker)

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate   # or: python3.11 -m venv .venv
uv pip install -r requirements.txt && pip install -e .     # `-e .` installs the `rag` command
cp .env.example .env                                       # edit the LLM_* lines
rag serve                                                  # http://localhost:8000
```

OCR outside Docker needs the Tesseract binary (`brew install tesseract` / `apt install
tesseract-ocr`). Without it the app still runs: scanned pages are skipped with a warning and
image uploads end as `FAILED` with a clear reason.

### Offline demo without an LLM key

`scripts/mock_llm.py` is a tiny OpenAI-compatible server that answers **extractively** from the
retrieved context (it is not a language model — for demos and CI only):

```bash
python scripts/mock_llm.py &     # http://localhost:8001/v1
# .env:  LLM_API_URL=http://localhost:8001/v1   LLM_API_KEY=mock   LLM_MODEL=mock-extractive
rag serve
```

## 2. How the upload job flow works

Uploading is asynchronous. Every file in a request becomes a **job** and the API returns
immediately (HTTP 202) with one `job_id` per file. A background worker indexes the file; you
poll `GET /api/jobs/{job_id}` or watch the listing. The interactive walkthrough is in
[`documentation/flow.html`](documentation/flow.html).

The listing (`GET /api/documents`, and the **Requests & status** table in the UI) shows
**DOC ID · NAME · STATUS · REASON** for every request ever made, including rejected ones.

| Status | Meaning | Processed? | REASON column |
|---|---|---|---|
| `QUEUED` | stored, waiting for the worker | soon | "Waiting for the background worker" |
| `DUPLICATE` | same SHA-256 as a live document in this collection | **no** | original's name + id, linked in the listing |
| `PROCESSING` | worker running; `stage` = extracting → chunking → embedding | in progress | current stage and counts |
| `COMPLETED` | indexed and searchable | yes | "Indexed N pages (k via OCR) into M chunks" |
| `FAILED` | processing raised an error | no | `PROCESSING FAILED: <exception>` |
| `EMPTY_FILE` | 0-byte upload | no | "The uploaded file is 0 bytes" |
| `EXTRACTION_NOT_SUPPORTED` | extension not in `SUPPORTED_EXTENSIONS` | no | the extension and the supported list |
| `UPLOAD_FAILED` | unreadable stream, larger than `MAX_UPLOAD_MB`, or disk write error | no | `UPLOAD FAILED: <cause>` |

Terminal statuses are all of them except `QUEUED` and `PROCESSING`. Duplicate detection is
per collection: the same file in another collection is a separate document set. Jobs caught
mid-flight by a restart are marked `FAILED` with an explanatory reason.

## 3. Using it

### UI (<http://localhost:8000>)
1. **Upload** — drop files, optionally name a *collection* (a document set). Accepted extensions are shown.
2. **Requests & status** — every job with its Doc ID, name, status pill and reason; duplicates link to their original; delete finished jobs with ✕.
3. **Ask** — the answer cites `[n]` blocks; click a citation to jump to the chunk.
4. **Sources** — each retrieved chunk with file, page/section, whether it came from dense or BM25 (or both) and its RRF score. OCR'd chunks are flagged.

**Reset collection** wipes a document set so you can load a different one.

### CLI
```bash
rag ingest samples/*.md samples/*.pdf --collection demo   # prints job ids, waits for terminal status
rag status --watch                                        # DOC ID · NAME · STATUS · REASON
rag job <job_id>                                          # one job in detail
rag ask "How many days of annual leave do employees get?" -c demo --show-sources
rag reset -c demo --yes
rag serve
```
The CLI talks to the API at `RAG_API_URL` (default `http://localhost:8000`, override with `--api`).

### REST API
| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | status, models, supported extensions, status vocabulary, collections, non-secret settings |
| `POST` | `/api/documents` | multipart `files[]` (+ `collection`) → **202** with one job per file |
| `GET` | `/api/jobs/{job_id}` | poll one job (`job_id` = `doc_id`) |
| `GET` | `/api/documents?collection=&status=` | listing of all requests: doc id, name, status, reason, duplicate link |
| `GET` | `/api/documents/{doc_id}` | same record as `/api/jobs/{id}` |
| `DELETE` | `/api/documents/{doc_id}` | remove a finished job and its chunks |
| `GET` | `/api/chunks?collection=&limit=&offset=` | inspect indexed chunks |
| `POST` | `/api/ask` | `{"question", "collection"?, "top_k"?}` → answer + cited sources |
| `DELETE` | `/api/collections/{name}` | reset a whole document set |

```bash
curl -F "files=@samples/handbook.md" -F collection=demo localhost:8000/api/documents
curl localhost:8000/api/jobs/<job_id>
curl -X POST localhost:8000/api/ask -H 'content-type: application/json' \
     -d '{"question":"What is the meal allowance?","collection":"demo"}'
```

## 4. What is supported

- **Input formats:** `.pdf .docx .html .htm .txt .md .csv` plus `.png .jpg .jpeg .tiff .tif` (OCR).
  Scanned PDF pages (fewer than `OCR_MIN_CHARS_PER_PAGE` extractable characters) fall back to OCR automatically.
- **Page metadata:** PDF = printed page · DOCX = one page per heading section · HTML = one page, `<title>` as section ·
  MD = one page per heading · TXT = one page · CSV = one page per `CSV_ROWS_PER_PAGE` rows (header repeated) · images = one page per frame.
- **Multiple document sets** via collections; reset or delete without restarting.
- **Any OpenAI-compatible LLM**; **any fastembed model**; CPU-only inference.
- **Multi-file uploads**, up to `MAX_UPLOAD_MB` (50 MB) per file.
- **Grounded answers** with citations and an explicit "I could not find this in the provided documents." fallback.

## 5. What is not supported (by design, for this version)

- Non-OpenAI-compatible generation APIs (e.g. raw Anthropic Messages API for generation — use a gateway such as OpenRouter or LiteLLM). The **judge** is Claude via the official SDK.
- Cross-encoder reranking, query rewriting, multi-hop or conversational memory: single-turn retrieval → answer only.
- Authentication, multi-tenancy, rate limiting — run behind your own gateway.
- Horizontal scaling: BM25 lives in one process's memory; run a single replica.
- Formats outside the list above (PPTX, XLSX, EPUB, audio…). Adding one is a small extractor — see Tuning.
- OCR languages other than English unless you install extra Tesseract language packs and set `OCR_LANG`.
- Real-time streaming of the answer (responses are returned whole).

## 6. Architecture

Full description with diagrams: [`documentation/architecture.md`](documentation/architecture.md)
(browser version: [`architecture.html`](documentation/architecture.html)). In short:

```
upload ─► validate/hash ─► QUEUED ─► worker: extract (pypdf/docx/bs4/csv/OCR) ─► chunk ─► fastembed ─► ChromaDB + BM25 ─► COMPLETED
ask    ─► embed ─► Chroma top-k ─┐
       └► tokenize ─► BM25 top-k ─┴─► RRF ─► top FUSED_TOP_K ─► grounded prompt ─► OpenAI-compatible LLM ─► answer + sources
```

## 7. Tuning — everything lives in `app/config.py`

Every tunable is a field on `Settings` with a default, overridable from `.env` (same name);
`.env.example` lists all of them. Nothing tunable is hard-coded elsewhere.

| Setting | Default | What it does |
|---|---|---|
| `CHUNK_SIZE_CHARS` / `CHUNK_OVERLAP_CHARS` | 1200 / 200 | window size and overlap for chunking |
| `CSV_ROWS_PER_PAGE` | 50 | rows per CSV "page" |
| `DENSE_TOP_K` / `BM25_TOP_K` | 20 / 20 | candidates from each retriever before fusion |
| `FUSED_TOP_K` | 6 | chunks passed to the LLM |
| `RRF_K` | 60 | RRF smoothing constant |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | any fastembed model (clear `data/chroma` after changing) |
| `OCR_ENABLED` / `OCR_MIN_CHARS_PER_PAGE` / `OCR_LANG` / `OCR_DPI` | true / 20 / eng / 200 | when and how the Tesseract fallback runs |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `LLM_TIMEOUT_S` | 0.1 / 1024 / 60 | generation parameters |
| `MAX_UPLOAD_MB` / `DEFAULT_COLLECTION` | 50 / `default` | upload limit, collection used when none is given |
| `APP_PORT` / `APP_HOST` | 8000 / `0.0.0.0` | port the app listens on (`rag serve`) and the host port docker compose publishes (`${APP_PORT:-8000}`) |
| `JUDGE_MODEL` / `EVAL_MIN_FAITHFULNESS` | `claude-opus-5` / 3.5 | evaluation |

**Supported formats** are the `SUPPORTED_EXTENSIONS` dict at the bottom of `app/config.py`
(extension → extractor name). Remove an entry to stop accepting a type. To add a format: write an
extractor in `app/ingest/extractors.py`, register it in `EXTRACTORS`, add the extension, add a test,
and update the docs.

## 8. Evaluation (Claude as judge)

```bash
rag ingest samples/* --collection default                 # index the corpus named in the dataset
# set ANTHROPIC_API_KEY in .env, then:
python -m evaluation.run_eval --dataset evaluation/dataset.example.jsonl
```

For each row the script calls `/api/ask`, then asks Claude (`JUDGE_MODEL`, official `anthropic`
SDK, structured outputs) to score **faithfulness**, **answer relevance**, **context relevance**,
**correctness vs. reference** (1–5) and flag **hallucination**. Reports land in
`evaluation/reports/<timestamp>.{json,md}`; the run exits non-zero when mean faithfulness is
below `--min-faithfulness`. For a new document set, synthesise questions first:

```bash
python -m evaluation.generate_dataset --collection mydocs --chunks 10 --per-chunk 2 --out evaluation/dataset.mydocs.jsonl
python -m evaluation.run_eval --dataset evaluation/dataset.mydocs.jsonl
```

## 9. Tech stack

**Used**

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | required by the brief; broad library support |
| API | FastAPI + uvicorn, pydantic, pydantic-settings | async uploads, background tasks, validation, free OpenAPI docs, `.env` settings |
| UI | one static `index.html`, vanilla JS | no build step; the brief asked for a single page |
| CLI | typer + httpx | talks to the API so only one process opens Chroma |
| Extraction | pypdf, python-docx, beautifulsoup4 + lxml, stdlib csv | one small library per format |
| OCR | Tesseract via pytesseract, pypdfium2 (page rendering), Pillow | fallback for scanned PDFs and images; no poppler dependency |
| Chunking | custom sliding window | overlapping, boundary-aware, metadata-tagged as required |
| Embeddings | fastembed `BAAI/bge-small-en-v1.5` (ONNX, CPU) | fast, no GPU, no API key |
| Vector DB | ChromaDB embedded `PersistentClient` | required by the brief; zero extra services |
| Lexical | rank-bm25 | required by the brief |
| Fusion | Reciprocal Rank Fusion | rank-based, robust to incomparable score scales |
| LLM | OpenAI-compatible `chat/completions` over httpx | works with every major provider by changing two `.env` lines |
| Judge | anthropic SDK, `claude-opus-5`, `messages.parse` | Claude as judge with validated structured verdicts |
| Docs tooling | python-markdown (`scripts/build_docs.py`) | HTML twins of Markdown docs |
| Tests | pytest + FastAPI TestClient | end-to-end without network or keys |
| Packaging | Docker (python:3.11-slim + tesseract), docker compose | required by the brief |

**Deliberately not used:** LangChain / LlamaIndex (a thin custom pipeline is easier to read and
tune), a cross-encoder reranker (out of scope; slots in after `hybrid_retrieve`), a separate
Chroma server or a job queue like Celery/Redis (FastAPI background tasks suffice for one
replica), a JS framework, GPU acceleration, the server-side refusal-fallback beta on the judge.

## 10. Samples and tests

`samples/` is a small fictional company corpus covering every format: `handbook.md`,
`security_policy.txt`, `robot_catalog.csv`, `release_notes.html`, `warranty.pdf`, `onboarding.docx`,
plus `scanned_memo.pdf` and `whiteboard.png` whose text exists only as pixels (OCR path). Regenerate
the binary ones with `pip install -r requirements-dev.txt && python scripts/make_samples.py`.

```bash
pytest   # chunker, RRF, every extractor, OCR-unavailable path, async job flow, duplicates, rejections, 502 on LLM errors
```

## 11. Repository layout

```
app/            FastAPI service (config, models, ingest/, retrieval/, llm/, rag.py, jobs.py, main.py, static/index.html)
cli/            `rag` CLI
evaluation/     Claude-as-judge runner, dataset generator, example dataset, reports/
documentation/  architecture.md(.html), flow.html (interactive), changelog.html, readme.html
samples/        demo corpus     scripts/  make_samples.py, mock_llm.py, build_docs.py, sync_rules.py
tests/          pytest suite    transcripts/  agent session exports
CLAUDE.md       rules for AI agents (source of truth) → synced to .cursor/rules/project.mdc and AGENTS.md
memory.md       project memory: decisions, gotchas, conventions
```

## 12. Working on this repo with an AI agent

`CLAUDE.md` (mirrored to `.cursor/rules/project.mdc` and `AGENTS.md` by `scripts/sync_rules.py`)
requires agents to read `memory.md` before planning, run the compile check and tests after every
change, never commit (the user commits), and update README / changelog / architecture / flow /
memory and the HTML twins with every code change.

## 13. Transcripts

`transcripts/` holds the complete Claude Code transcripts of the sessions that built this project
(see `transcripts/README.md`).
