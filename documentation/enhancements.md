# Enhancements

Improvements that would make RAG Generator more capable or production-ready, in rough
priority order. None are needed for the assessment scope; each is listed with why it matters
and where it would plug in.

## Retrieval quality
1. **Cross-encoder reranking** after RRF (e.g. `BAAI/bge-reranker-base` via fastembed's rerankers)
   to reorder the fused top-20 before taking `FUSED_TOP_K`. Plugs in at the end of `hybrid_retrieve`.
2. **Query rewriting / HyDE**: let the LLM expand or rephrase the question before retrieval
   to close vocabulary gaps between question and documents.
3. **Semantic chunking** (embedding-similarity boundaries or heading-aware sizes) as an
   alternative to the fixed character window; expose the strategy as a setting.
4. **Parent-child chunks**: retrieve on small chunks, hand the LLM the enclosing section
   for more context without hurting recall.
5. **Metadata filters in questions**: allow `page`, `section`, `file_type` filters on
   `/api/ask` (the store already carries this metadata).
6. **Hybrid weighting**: weighted RRF or per-collection tuning of `DENSE_TOP_K` / `BM25_TOP_K`
   driven by the evaluation script.

## Answering
7. **Streaming answers** (Server-Sent Events) so long answers appear progressively in the UI.
8. **Conversational memory**: multi-turn sessions with condensed history and follow-up questions.
9. **Answer-level confidence**: surface the RRF score spread or a judge-lite check to flag
   weakly grounded answers.
10. **Native Anthropic Messages API client** as a second `LLM_PROVIDER` next to the
    OpenAI-compatible one, with citations blocks mapped to the same `citations` payload.

## Ingestion
11. **Real job queue** (Redis + a worker process, or Celery/RQ) so ingestion survives
    restarts and scales past one process; the `jobs` / `documents` stores already model this.
12. **More formats**: PPTX, XLSX, EPUB, EML, JSON/JSONL, plus URL ingestion (fetch + HTML extractor).
13. **Incremental re-index**: re-upload replaces an existing document instead of being
    a duplicate, with a version counter in the `documents` store.
14. **OCR improvements**: language auto-detection, table-aware OCR, image pre-processing
    (deskew / binarise) before Tesseract; optional cloud OCR provider.
15. **Per-file progress**: percentage of pages processed exposed on the job record.

## Storage and scale
16. **Persisted BM25** (or SQLite FTS5 / Tantivy) instead of rebuilding in memory at startup.
17. **Chroma server or pgvector** deployment profile in `docker-compose.yml` for multi-replica setups.
18. **SQLite for `jobs` / `documents`** instead of JSON files once listings grow large;
    pagination is already API-level so the switch is internal.

## Operations
19. **Authentication and multi-tenancy**: API keys or OIDC, collections owned by tenants.
20. **Observability**: structured logs, Prometheus metrics (ingest latency, retrieval hit
    rate, LLM latency), request tracing.
21. **Rate limiting and upload quotas** per collection.
22. **CI pipeline**: GitHub Actions running `pytest`, `scripts/sync_rules.py --check`,
    `scripts/build_docs.py` drift check, and a Docker build.

## Evaluation
23. **Retrieval-only metrics** (recall@k, MRR against labelled chunks) alongside the judge,
    so retrieval and generation regressions are separable.
24. **Eval in CI** with the mock LLM for retrieval and a nightly judged run with the real LLM.
25. **Hill-climbing harness**: sweep chunk size / top-k / RRF_K and report the judge scores per configuration.

## UI
26. Bulk selection helpers (select all COMPLETED on this page / in this job), status filter
    dropdown, search box over the document listing.
27. Highlight the cited passage inside the source chunk; show page thumbnails for PDFs.
28. Dark mode and keyboard shortcuts.
