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
        job = r.json()
        typer.secho(f"job {job['job_id']}  {job['status']}  ({job['files']} files, collection {job['collection']})", fg="cyan")
        for d in job["documents"]:
            typer.echo(f"  {d['status']:26} {d['filename']}  doc_id={d['doc_id']}")
        if not wait or job["status"] in ("COMPLETED", "FAILED"):
            return
        while True:
            job = c.get(f"/api/jobs/{job['job_id']}").json()
            if job["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(1)
        typer.secho(f"job {job['job_id']}  {job['status']}  {job['reason']}",
                    fg="green" if job["status"] == "COMPLETED" else "red")
        for d in job["documents"]:
            color = "green" if d["status"] == "COMPLETED" else ("yellow" if d["status"] == "DUPLICATE" else "red")
            typer.secho(f"  {d['status']:26} {d['filename']}  {d.get('reason') or ''}", fg=color)


@app.command()
def status(collection: Optional[str] = COL, api: Optional[str] = API,
           job: Optional[str] = typer.Option(None, "--job", "-j", help="Only documents of this job"),
           page: int = typer.Option(0, help="0-based page"), size: int = typer.Option(5, help="page size"),
           watch: bool = typer.Option(False, help="Refresh every second until nothing is processing")):
    """List documents (DOC ID · NAME · STATUS · REASON), paginated, optionally filtered by job."""
    with _client(api) as c:
        while True:
            params = {k: v for k, v in {"collection": collection, "job_id": job}.items() if v}
            params.update({"page": page, "size": size})
            r = c.get("/api/documents", params=params)
            if r.status_code >= 400:
                _die(r)
            body = r.json()
            docs = body["items"]
            if not docs:
                typer.echo("no documents")
            else:
                typer.secho(f"page {body['page'] + 1}/{body['pages']} · {body['total']} document(s) · size {body['size']}", fg="bright_black")
                typer.secho(f"{'DOC ID':12} {'NAME':32} {'PAGES':5} {'STATUS':26} REASON", fg="bright_black")
            for d in docs:
                dup = f" -> original {d['duplicate_of']}" if d.get("duplicate_of") else ""
                stage = f" ({d['stage']})" if d.get("stage") else ""
                pages = str(d["pages"]) if d.get("pages") else "-"
                typer.echo(f"{d['doc_id']:12} {d['filename'][:32]:32} {pages:5} {d['status'] + stage:26} "
                           f"{d.get('reason') or ''}{dup}")
            busy = any(d["status"] not in TERMINAL for d in docs)
            if not watch or not busy:
                break
            time.sleep(1)
            typer.echo("---")


@app.command()
def jobs(collection: Optional[str] = COL, api: Optional[str] = API,
         page: int = typer.Option(0, help="0-based page"), size: int = typer.Option(5, help="page size")):
    """List upload jobs (JOB ID · FILES · STATUS · REASON), paginated."""
    with _client(api) as c:
        params = {"page": page, "size": size}
        if collection:
            params["collection"] = collection
        r = c.get("/api/jobs", params=params)
        if r.status_code >= 400:
            _die(r)
        body = r.json()
        rows = body["items"]
        if not rows:
            typer.echo("no jobs")
            return
        typer.secho(f"page {body['page'] + 1}/{body['pages']} · {body['total']} job(s) · size {body['size']}", fg="bright_black")
        typer.secho(f"{'JOB ID':12} {'DOCS':5} {'PAGES':5} {'STATUS':10} {'COLLECTION':12} REASON", fg="bright_black")
        for j in rows:
            pages = j["total_pages"] if j["counts"].get("COMPLETED") else "-"
            typer.echo(f"{j['job_id']:12} {j['total_documents']:<5} {str(pages):5} {j['status']:10} "
                       f"{j['collection']:12} {j['reason']}")


@app.command(name="job")
def job(job_id: str, api: Optional[str] = API):
    """Show one upload job and the documents it contains."""
    with _client(api) as c:
        r = c.get(f"/api/jobs/{job_id}")
        if r.status_code >= 400:
            _die(r)
        j = r.json()
        typer.echo(f"job {j['job_id']}  {j['status']}  {j['reason']}  (collection {j['collection']}, "
                   f"{j['total_documents']} documents, {j['total_pages']} pages, {j['total_chunks']} chunks)")
        for d in j["documents"]:
            dup = f" -> original {d['duplicate_of']}" if d.get("duplicate_of") else ""
            pages = f"{d['pages']}p" if d.get("pages") else "-"
            typer.echo(f"  {d['doc_id']:12} {d['filename'][:32]:32} {pages:5} {d['status']:26} {d.get('reason') or ''}{dup}")


@app.command()
def ask(question: str, collection: Optional[str] = COL, api: Optional[str] = API,
        doc: list[str] = typer.Option([], "--doc", "-d", help="Restrict to this document id (repeatable)"),
        job: list[str] = typer.Option([], "--job", "-j", help="Restrict to the documents of this job (repeatable)"),
        top_k: Optional[int] = typer.Option(None, help="Chunks passed to the LLM"),
        show_sources: bool = typer.Option(False, "--show-sources", "-s", help="Print every retrieved chunk")):
    """Ask a question over the collection, or only over selected documents / jobs.

    Prints the answer text, then the citations it uses (file, page, section)."""
    with _client(api) as c:
        payload = {"question": question, "collection": collection, "top_k": top_k,
                   "doc_ids": doc or None, "job_ids": job or None}
        r = c.post("/api/ask", json=payload)
        if r.status_code >= 400:
            _die(r)
        res = r.json()
        typer.echo(res["answer"])
        scope = res["scope"]
        scope_txt = (f"{len(scope['doc_ids'])} selected document(s)" if scope["restricted"]
                     else f"whole collection '{scope['collection']}'")
        typer.secho(f"\n[{res['model']} · {res['latency_ms']} ms · scope: {scope_txt} · "
                    f"{len(res['citations'])} citation(s) from {len(res['sources'])} retrieved chunks]", fg="bright_black")
        if res["unresolved_citations"]:
            typer.secho(f"note: removed citation markers with no source: {res['unresolved_citations']}", fg="yellow")
        for s in res["citations"]:
            loc = f"page {s['page']}" + (f", {s['section']}" if s.get("section") else "")
            typer.secho(f"  [{s['n']}] {s['source']} — {loc}  (doc {s['doc_id']})", fg="cyan")
        if show_sources:
            typer.secho("\nAll retrieved chunks:", fg="bright_black")
            for s in res["sources"]:
                loc = f"page {s['page']}" + (f", {s['section']}" if s.get("section") else "")
                tag = " (OCR)" if s.get("extraction") == "ocr" else ""
                typer.secho(f"\n[{s['n']}] {s['source']} — {loc}{tag}  dense#{s['dense_rank']} "
                            f"bm25#{s['bm25_rank']} rrf={s['rrf_score']}", fg="cyan")
                typer.echo(s["text"])


@app.command()
def retry(doc_id: str, api: Optional[str] = API, wait: bool = typer.Option(True, help="Wait for the result")):
    """Re-process a FAILED document from its stored file."""
    with _client(api) as c:
        r = c.post(f"/api/documents/{doc_id}/retry")
        if r.status_code >= 400:
            _die(r)
        d = r.json()
        typer.echo(f"{d['status']:26} {d['filename']}  {d.get('reason') or ''}")
        if not wait:
            return
        while d["status"] not in TERMINAL:
            time.sleep(1)
            d = c.get(f"/api/documents/{doc_id}").json()
        typer.secho(f"{d['status']:26} {d['filename']}  {d.get('reason') or ''}",
                    fg="green" if d["status"] == "COMPLETED" else "red")


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
