"""
`rag` command line. Talks to the running API so there is a single Chroma writer.

  rag ingest docs/*.pdf --collection legal
  rag status --watch
  rag ask "What is the notice period?" --show-sources
  rag reset --collection legal
  rag serve
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from app.config import settings
from app.models import TERMINAL_STATUSES

TERMINAL = TERMINAL_STATUSES

app = typer.Typer(help="RAG Generator CLI", no_args_is_help=True, add_completion=False)
API = typer.Option(None, "--api", help="API base URL (default: RAG_API_URL from .env)")
COL = typer.Option(None, "--collection", "-c", help="Collection / document set name")


def _client(api: Optional[str]) -> httpx.Client:
    return httpx.Client(base_url=(api or settings.RAG_API_URL).rstrip("/"), timeout=120)


def _die(resp: httpx.Response) -> None:
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.secho(f"HTTP {resp.status_code}: {detail}", fg="red", err=True)
    raise typer.Exit(1)


@app.command()
def ingest(files: list[Path] = typer.Argument(..., exists=True, readable=True),
           collection: Optional[str] = COL, api: Optional[str] = API,
           wait: bool = typer.Option(True, help="Wait until indexing finishes")):
    """Upload one or more files and (by default) wait for them to be indexed."""
    col = collection or settings.DEFAULT_COLLECTION
    with _client(api) as c:
        payload = [("files", (p.name, p.read_bytes())) for p in files]
        r = c.post("/api/documents", files=payload, data={"collection": col})
        if r.status_code >= 400:
            _die(r)
        body = r.json()
        for d in body["documents"]:
            typer.echo(f"{d['status']:26} {d['filename']}  job_id={d['job_id']}")
        ids = [d["job_id"] for d in body["documents"] if d["status"] not in TERMINAL]
        if not wait or not ids:
            return
        while True:
            mine = [c.get(f"/api/jobs/{i}").json() for i in ids]
            if all(d["status"] in TERMINAL for d in mine):
                break
            time.sleep(1)
        for d in mine:
            color = "green" if d["status"] == "COMPLETED" else "red"
            typer.secho(f"{d['status']:26} {d['filename']}  {d.get('reason') or ''}", fg=color)


@app.command()
def status(collection: Optional[str] = COL, api: Optional[str] = API,
           watch: bool = typer.Option(False, help="Refresh every second until nothing is processing")):
    """List documents and their indexing status."""
    with _client(api) as c:
        while True:
            r = c.get("/api/documents", params={"collection": collection} if collection else None)
            if r.status_code >= 400:
                _die(r)
            docs = r.json()
            if not docs:
                typer.echo("no documents")
            else:
                typer.secho(f"{'DOC ID':12} {'NAME':32} {'STATUS':26} REASON", fg="bright_black")
            for d in docs:
                dup = f" -> original {d['duplicate_of']}" if d.get("duplicate_of") else ""
                stage = f" ({d['stage']})" if d.get("stage") else ""
                typer.echo(f"{d['doc_id']:12} {d['filename'][:32]:32} {d['status'] + stage:26} "
                           f"{d.get('reason') or ''}{dup}")
            busy = any(d["status"] not in TERMINAL for d in docs)
            if not watch or not busy:
                break
            time.sleep(1)
            typer.echo("---")


@app.command(name="job")
def job(job_id: str, api: Optional[str] = API):
    """Check the status of one upload job by its job id."""
    with _client(api) as c:
        r = c.get(f"/api/jobs/{job_id}")
        if r.status_code >= 400:
            _die(r)
        d = r.json()
        for k in ("job_id", "filename", "collection", "status", "stage", "reason", "duplicate_of",
                  "pages", "chunks", "ocr_pages", "created_at", "updated_at"):
            if d.get(k) not in (None, "", 0):
                typer.echo(f"{k:14} {d[k]}")


@app.command()
def ask(question: str, collection: Optional[str] = COL, api: Optional[str] = API,
        top_k: Optional[int] = typer.Option(None, help="Chunks passed to the LLM"),
        show_sources: bool = typer.Option(False, "--show-sources", "-s")):
    """Ask a question; prints the grounded answer (and optionally the cited chunks)."""
    with _client(api) as c:
        r = c.post("/api/ask", json={"question": question, "collection": collection, "top_k": top_k})
        if r.status_code >= 400:
            _die(r)
        res = r.json()
        typer.echo(res["answer"])
        typer.secho(f"\n[{res['model']} · {res['latency_ms']} ms · {len(res['sources'])} sources]",
                    fg="bright_black")
        if show_sources:
            for s in res["sources"]:
                loc = f"page {s['page']}" + (f", {s['section']}" if s.get("section") else "")
                tag = " (OCR)" if s.get("extraction") == "ocr" else ""
                typer.secho(f"\n[{s['n']}] {s['source']} — {loc}{tag}  dense#{s['dense_rank']} "
                            f"bm25#{s['bm25_rank']} rrf={s['rrf_score']}", fg="cyan")
                typer.echo(s["text"])


@app.command()
def reset(collection: Optional[str] = COL, api: Optional[str] = API,
          yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation")):
    """Delete every document and vector in a collection."""
    col = collection or settings.DEFAULT_COLLECTION
    if not yes and not typer.confirm(f"Delete all documents in collection '{col}'?"):
        raise typer.Exit(0)
    with _client(api) as c:
        r = c.delete(f"/api/collections/{col}")
        if r.status_code >= 400:
            _die(r)
    typer.secho(f"collection '{col}' reset", fg="green")


@app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None),
          reload: bool = typer.Option(False)):
    """Run the API + UI locally with uvicorn."""
    import uvicorn

    uvicorn.run("app.main:app", host=host or settings.APP_HOST, port=port or settings.APP_PORT,
                reload=reload)


if __name__ == "__main__":
    sys.exit(app())
