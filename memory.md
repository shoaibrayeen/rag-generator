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
- **BM25 in memory, rebuilt from Chroma** — single source of truth; requires one app process.
- **RRF (k=60)** for fusion — dense distances and BM25 scores are on incomparable scales.
- **fastembed only embeds**; chunking is our own boundary-aware sliding window.
- **Duplicate detection is per collection** by SHA-256 of the bytes; the same file in another
  collection is a legitimate separate document set.
- **Rejected uploads are records, not errors** — the listing must explain every request
  (DOC ID · NAME · STATUS · REASON), per the brief.
- **Tesseract OCR as a fallback only** — triggered when a PDF page has fewer than
  `OCR_MIN_CHARS_PER_PAGE` characters, and for image uploads. Missing binary degrades gracefully.
- **Server-side refusal-fallback beta not used on the judge** — benign prompts, simpler script.

## Environment gotchas
- The dev Mac has no Docker, Homebrew or Tesseract; Python is 3.9 system-wide. Use
  `uv venv --python 3.11 .venv`. Docker image must be built/verified elsewhere.
- Locally OCR is therefore unavailable: scanned sample pages are tagged `ocr_unavailable`
  and `scanned_memo.pdf` ends `FAILED` ("No text could be extracted") — expected without Tesseract.
- `scripts/mock_llm.py` (port 8001) is an extractive stand-in for demos/CI. It is not a model.
- First embedding-model load downloads ~130 MB; the Dockerfile pre-downloads it.
- fastembed + chromadb pin numpy/onnxruntime jointly; regenerate `requirements.txt` with
  `uv pip compile requirements.in -o requirements.txt`.

## Conventions
- All tunables in `app/config.py`; `.env.example` mirrors every field.
- `job_id == doc_id`. Terminal statuses: everything except `QUEUED` and `PROCESSING`.
- Tests mock `app.rag.llm.chat`; no network needed.
- Version lives in `app/main.py` (`FastAPI(version=…)`) and `documentation/changelog.html`.
- Docs live in `documentation/` (the user asked for this name, not `docs/`; `/docs` stays Swagger).
  Every `.md` there and `README.md` gets an `.html` twin via `scripts/build_docs.py`.

## History
- 2026-09-06 — v0.1.0 initial build; v0.2.0 async job model with fixed statuses, dedupe, docs set.
