# RAG Generator

**What it does:** you upload documents while the service is running; it extracts their text
(with page metadata), splits it into overlapping, metadata-tagged chunks, embeds them, and
indexes them for **hybrid retrieval** (vector search + BM25, fused with Reciprocal Rank
Fusion). You then ask questions in a web UI, CLI or REST call and get an answer **grounded in
your documents with `[n]` citations** to the exact file, page and passage. Swap the documents
for a completely different set and it works unchanged — no code edits, only `.env`.

| | |
|---|---|
| Interface | REST API (FastAPI) · single-page UI (upload · jobs · documents · ask · sources) · `rag` CLI |
| Ingestion | asynchronous **jobs**: one upload request = one job → `job_id` → poll; job listing and per-file document listing with a fixed status vocabulary and reasons; SHA-256 duplicate detection |
| Asking | over the whole collection **or scoped to selected documents and/or jobs**; answer = text with `[n]` markers + validated `citations` + all retrieved `sources` |
| Formats | PDF (PyMuPDF), DOCX (python-docx), HTML (beautifulsoup4), TXT/MD/CSV (stdlib) → text with page metadata · **Tesseract OCR as fallback** when a primary extractor fails or finds no text · PNG/JPG/TIFF via OCR |
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

Uploading is asynchronous. **One upload request (any number of files) = one job.** The API
returns immediately (HTTP 202) with the job — its `job_id`, a derived job status, and one
document record per file. A background worker indexes the files; poll `GET /api/jobs/{job_id}`
or watch the listings. The interactive walkthrough is in
[`documentation/flow.html`](documentation/flow.html).

Two listings:

- **Jobs** (`GET /api/jobs`, UI panel 2): **JOB ID · FILES · STATUS · REASON**. Job status is
  derived from its documents every time it is read: `QUEUED` (nothing started), `PROCESSING`
  (any file still running), `COMPLETED` (all files finished and at least one indexed or
  duplicate; reason says "partially completed: …" when some files failed) or `FAILED` (all
  files finished, none indexed). Clicking a job in the UI filters the document listing to that
  job (`GET /api/documents?job_id=…`).
- **Documents** (`GET /api/documents`, UI panel 3): **DOC ID · NAME · STATUS · REASON** for every
  file ever uploaded, including rejected ones, with a link from a duplicate to its original.

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
per collection: the same file in another collection is a separate document set. Documents caught
mid-flight by a restart are marked `FAILED` with an explanatory reason.

### Asking over selected documents or jobs

`POST /api/ask` accepts `doc_ids` and/or `job_ids`. Without them the whole collection is
searched. With them, both retrievers (dense and BM25) are restricted to exactly those
documents (a job expands to its `COMPLETED` documents; a duplicate id resolves to its original).
The request is validated: unknown id → 404, a document or job that is not `COMPLETED` → 409,
ids spanning several collections or contradicting `collection` → 400.

The response always contains the **answer text** (with `[n]` markers), **`citations`** — the
sources those markers actually refer to, in order of first use — plus **`sources`** (everything
retrieved) and **`scope`** (what was searched). Markers the LLM produced that match no retrieved
source are removed from the text and reported in `unresolved_citations`, so a client never shows a
citation it cannot open.

## 3. Using it

### UI (<http://localhost:8000>)
1. **Upload** — drop files, optionally name a *collection* (a document set); they are submitted as one job and the job id is shown.
2. **Jobs** — JOB ID · FILES · STATUS · REASON. Click a row to filter the documents to that job; tick a job to ask over it.
3. **Documents** — DOC ID · NAME · STATUS · REASON for every file (job id shown under the doc id); duplicates link to their original; tick `COMPLETED` documents to ask over them; delete finished ones with ✕.
4. **Ask** — the scope line shows what will be searched (whole collection or the ticked jobs/documents). The answer shows `[n]` markers, then a **Citations** list (file, page, section, doc id); click a marker to jump to the chunk.
5. **Sources** — every retrieved chunk with file, page/section, doc id, whether it came from dense or BM25 (or both), its RRF score and a "cited" badge. OCR'd chunks are flagged.

**Reset collection** wipes a document set so you can load a different one.

### CLI
```bash
rag ingest samples/*.md samples/*.pdf --collection demo   # one job; prints the job id, waits for it
rag jobs                                                  # JOB ID · FILES · STATUS · REASON
rag job <job_id>                                          # one job and its documents
rag status --watch                                        # DOC ID · NAME · STATUS · REASON (all documents)
rag status --job <job_id>                                 # documents of one job
rag ask "How many days of annual leave do employees get?" -c demo          # whole collection
rag ask "What is the notice period?" --doc <doc_id> --doc <doc_id>         # only these documents
rag ask "Summarise the warranty terms" --job <job_id> --show-sources        # only this job's documents
rag reset -c demo --yes
rag serve
```
The CLI talks to the API at `RAG_API_URL` (default `http://localhost:8000`, override with `--api`).

### REST API
| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | status, models, supported extensions, status vocabulary, collections, non-secret settings |
| `POST` | `/api/documents` | multipart `files[]` (+ `collection`) → **202** with the **job** (`job_id`, status, one document record per file) |
| `GET` | `/api/jobs?collection=` | job listing: job id, files, status, reason, counts per status |
| `GET` | `/api/jobs/{job_id}` | poll one job, including its documents |
| `GET` | `/api/documents?collection=&job_id=&status=` | document listing: doc id, name, status, reason, duplicate link; filter by job |
| `GET` | `/api/documents/{doc_id}` | one document |
| `DELETE` | `/api/documents/{doc_id}` | remove a finished document and its chunks |
| `GET` | `/api/chunks?collection=&doc_id=&job_id=&limit=&offset=` | inspect the `document_chunks` store, filterable by document or job |
| `POST` | `/api/ask` | `{"question", "collection"?, "doc_ids"?, "job_ids"?, "top_k"?}` → `answer`, `citations`, `sources`, `scope`, `unresolved_citations` |
| `DELETE` | `/api/collections/{name}` | reset a whole document set |

```bash
curl -F "files=@samples/handbook.md" -F "files=@samples/warranty.pdf" -F collection=demo localhost:8000/api/documents
curl localhost:8000/api/jobs/<job_id>                       # job status + its documents
curl "localhost:8000/api/documents?job_id=<job_id>"         # documents of that job
curl -X POST localhost:8000/api/ask -H 'content-type: application/json' \
     -d '{"question":"What is the meal allowance?","collection":"demo"}'
curl -X POST localhost:8000/api/ask -H 'content-type: application/json' \
     -d '{"question":"How long is the Titan warranty?","job_ids":["<job_id>"]}'
```

## 4. What is supported

- **Input formats:** `.pdf .docx .html .htm .txt .md .csv` plus `.png .jpg .jpeg .tiff .tif` (OCR).
- **Two-tier extraction.** Primary extractors run first: PyMuPDF (PDF), python-docx (DOCX),
  beautifulsoup4 + lxml (HTML), stdlib (TXT/MD/CSV). **Tesseract is the fallback**, used only when a
  primary extractor raises or yields no text: scanned/image-only PDFs are rendered page by page
  (PyMuPDF) and OCR'd; a DOCX with no body text has its embedded images OCR'd; HTML that lxml cannot
  parse falls back to stdlib tag-stripping; individual scanned pages inside a digital PDF (fewer than
  `OCR_MIN_CHARS_PER_PAGE` characters) are OCR'd in place. Image files go straight to OCR.
  If neither tier yields text the job ends `FAILED` with the precise reason (e.g. Tesseract not installed).
- **Page metadata:** PDF = printed page · DOCX = one page per heading section · HTML = one page, `<title>` as section ·
  MD = one page per heading · TXT = one page · CSV = one page per `CSV_ROWS_PER_PAGE` rows (header repeated) · images = one page per frame.
- **Multiple document sets** via collections (names: 3-63 chars of letters, digits, `. _ -`); reset or delete without restarting.
- **Scoped questions** over selected documents and/or jobs, with validated citations in every answer.
- **Any OpenAI-compatible LLM**; **any fastembed model**; CPU-only inference.
- **Multi-file uploads**, up to `MAX_UPLOAD_MB` (50 MB) per file.
- **Grounded answers** with citations and an explicit "I could not find this in the provided documents." fallback.

## 5. What is not supported (by design, for this version)

- Non-OpenAI-compatible generation APIs (e.g. raw Anthropic Messages API for generation — use a gateway such as OpenRouter or LiteLLM). The **judge** is Claude via the official SDK.
- Cross-encoder reranking, query rewriting, multi-hop or conversational memory: single-turn retrieval → answer only.
- Asking across several collections in one question (a scope must live in one collection).
- Authentication, multi-tenancy, rate limiting — run behind your own gateway.
- Horizontal scaling: BM25 lives in one process's memory; run a single replica.
- Formats outside the list above (PPTX, XLSX, EPUB, audio…). Adding one is a small extractor — see Tuning.
- OCR of images embedded in HTML pages (only DOCX-embedded images and PDF pages are OCR'd).
- OCR languages other than English unless you install extra Tesseract language packs and set `OCR_LANG`.
- Real-time streaming of the answer (responses are returned whole).

## 6. Architecture

### The three stores

| Store | Where | Serves | Keyed / filtered by |
|---|---|---|---|
| `jobs` | `data/jobs.json` | job listing, `GET /api/jobs`, job status (derived) | `job_id`, `collection` |
| `documents` | `data/documents.json` | document listing, `GET /api/documents`, per-file status and reason | `doc_id`, `job_id`, `collection`, `status`, `sha256` |
| `document_chunks` | ChromaDB collection (`data/chroma/`) | chunking output: text + embedding + metadata for retrieval; `GET /api/chunks` | metadata `collection`, `doc_id`, `job_id`, `page`, `section`, `chunk_index` |

Every chunk carries `job_id` as well as `doc_id`, so filtering retrieval (or `/api/chunks`) by an
upload job needs no join. Names are settings (`JOBS_STORE`, `DOCUMENTS_STORE`, `CHUNKS_STORE`).

Full description with diagrams: [`documentation/architecture.md`](documentation/architecture.md)
(browser version: [`architecture.html`](documentation/architecture.html)). In short:

```
upload ─► job ─► per file: validate/hash ─► QUEUED ─► worker: extract (PyMuPDF/docx/bs4/stdlib → OCR fallback) ─► chunk ─► fastembed ─► ChromaDB + BM25 ─► COMPLETED
ask    ─► resolve scope (collection | doc_ids | job_ids) ─► embed ─► Chroma top-k ─┐
                                                         └► tokenize ─► BM25 top-k ─┴─► RRF ─► grounded prompt ─► LLM ─► answer + validated citations + sources
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
(extension → extractor name). Remove an entry to stop accepting a type. To add a format: write a primary
extractor (and optionally a fallback) in `app/ingest/extractors.py`, register them in `EXTRACTORS` /
`FALLBACKS`, add the extension, add a test, and update the docs.

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
| Extraction (primary) | PyMuPDF, python-docx, beautifulsoup4 + lxml, stdlib csv/html.parser | one small library per format; PyMuPDF also renders pages for OCR |
| OCR (fallback) | Tesseract via pytesseract + Pillow | runs only when the primary extractor fails or finds no text, and for images |
| Chunking | custom sliding window | overlapping, boundary-aware, metadata-tagged as required |
| Embeddings | fastembed `BAAI/bge-small-en-v1.5` (ONNX, CPU) | fast, no GPU, no API key |
| Vector DB | ChromaDB embedded `PersistentClient`, one `document_chunks` collection with metadata filters | required by the brief; zero extra services; jobs/documents/sets are metadata, not separate indexes |
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
app/            FastAPI service (config, models, ingest/, retrieval/, llm/, rag.py, jobs.py [jobs + documents registry], main.py, static/index.html)
cli/            `rag` CLI
evaluation/     Claude-as-judge runner, dataset generator, example dataset, reports/
documentation/  architecture.md(.html), flow.html (interactive), changelog.html, readme.html
samples/        demo corpus     scripts/  make_samples.py, mock_llm.py, build_docs.py, sync_rules.py
tests/          pytest suite    transcripts/  agent session exports    plans/  the approved implementation plan + change log
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
