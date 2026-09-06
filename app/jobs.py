"""Job / document registry: in-memory dict persisted to data/documents.json."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.models import TERMINAL_STATUSES, DocumentInfo, JobDetail, JobInfo

_docs: dict[str, DocumentInfo] = {}
_jobs: dict[str, dict] = {}   # job_id -> {job_id, collection, doc_ids, created_at}
_lock = threading.Lock()
_loaded = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    jpath = settings.jobs_file
    if jpath.exists():
        try:
            for raw in json.loads(jpath.read_text()):
                _jobs[raw["job_id"]] = raw
        except Exception:
            pass
    path = settings.documents_file
    if path.exists():
        try:
            for raw in json.loads(path.read_text()):
                doc = DocumentInfo(**raw)
                if doc.status not in TERMINAL_STATUSES:  # interrupted by a restart
                    doc.status, doc.stage = "FAILED", None
                    doc.reason = "Processing interrupted by a server restart; upload the file again."
                _docs[doc.doc_id] = doc
        except Exception:
            pass
    _loaded = True


def _save() -> None:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = settings.documents_file.with_suffix(".tmp")
    tmp.write_text(json.dumps([d.model_dump() for d in _docs.values()], indent=2))
    tmp.replace(settings.documents_file)
    jtmp = settings.jobs_file.with_suffix(".tmp")
    jtmp.write_text(json.dumps(list(_jobs.values()), indent=2))
    jtmp.replace(settings.jobs_file)


def create_job(job_id: str, collection: str) -> None:
    with _lock:
        _load()
        _jobs[job_id] = {"job_id": job_id, "collection": collection, "doc_ids": [], "created_at": _now()}
        _save()


def _job_view(raw: dict, with_docs: bool) -> JobInfo | JobDetail:
    docs = [_docs[d] for d in raw["doc_ids"] if d in _docs]
    counts: dict[str, int] = {}
    for d in docs:
        counts[d.status] = counts.get(d.status, 0) + 1
    if any(d.status == "PROCESSING" for d in docs):
        status = "PROCESSING"
    elif any(d.status == "QUEUED" for d in docs):
        status = "QUEUED" if all(d.status == "QUEUED" for d in docs) else "PROCESSING"
    elif counts.get("COMPLETED") or counts.get("DUPLICATE"):
        status = "COMPLETED"
    else:
        status = "FAILED"
    parts = [f"{n} {st.lower().replace('_', ' ')}" for st, n in sorted(counts.items())]
    reason = ", ".join(parts) if parts else "no files"
    if status == "COMPLETED" and any(st not in ("COMPLETED", "DUPLICATE") for st in counts):
        reason = "partially completed: " + reason
    if status == "FAILED":
        reason = "no file could be indexed: " + reason
    updated = max([raw["created_at"]] + [d.updated_at for d in docs])
    data = dict(job_id=raw["job_id"], collection=raw["collection"], status=status, reason=reason,
                files=len(docs), total_documents=len(docs),
                total_pages=sum(d.pages for d in docs if d.status == "COMPLETED"),
                total_chunks=sum(d.chunks for d in docs if d.status == "COMPLETED"),
                counts=counts, doc_ids=list(raw["doc_ids"]),
                created_at=raw["created_at"], updated_at=updated)
    return JobDetail(**data, documents=docs) if with_docs else JobInfo(**data)


def get_job(job_id: str) -> Optional[JobDetail]:
    with _lock:
        _load()
        raw = _jobs.get(job_id)
        return _job_view(raw, True) if raw else None


def list_jobs(collection: str | None = None) -> list[JobInfo]:
    with _lock:
        _load()
        views = [_job_view(r, False) for r in _jobs.values()
                 if collection is None or r["collection"] == collection]
        return sorted(views, key=lambda j: j.created_at, reverse=True)


def delete_job(job_id: str) -> None:
    with _lock:
        _load()
        if _jobs.pop(job_id, None) is not None:
            _save()


def create(doc_id: str, filename: str, collection: str, status: str, *, job_id: str,
           file_type: str | None = None, reason: str | None = None, sha256: str | None = None,
           duplicate_of: DocumentInfo | None = None) -> DocumentInfo:
    with _lock:
        _load()
        doc = DocumentInfo(job_id=job_id, doc_id=doc_id, filename=filename, file_type=file_type,
                           collection=collection, status=status, reason=reason, sha256=sha256,
                           duplicate_of=duplicate_of.doc_id if duplicate_of else None,
                           duplicate_of_name=duplicate_of.filename if duplicate_of else None,
                           created_at=_now(), updated_at=_now())
        _docs[doc_id] = doc
        if job_id in _jobs:
            _jobs[job_id]["doc_ids"].append(doc_id)
        _save()
        return doc


def update(doc_id: str, **fields) -> Optional[DocumentInfo]:
    with _lock:
        _load()
        doc = _docs.get(doc_id)
        if doc is None:
            return None
        for k, v in fields.items():
            setattr(doc, k, v)
        doc.updated_at = _now()
        _save()
        return doc


def get(doc_id: str) -> Optional[DocumentInfo]:
    with _lock:
        _load()
        return _docs.get(doc_id)


def find_by_hash(collection: str, sha256: str) -> Optional[DocumentInfo]:
    """The original a new upload would duplicate: same hash, same collection, still live."""
    with _lock:
        _load()
        for d in _docs.values():
            if (d.collection == collection and d.sha256 == sha256
                    and d.status in ("QUEUED", "PROCESSING", "COMPLETED")):
                return d
        return None


def list_docs(collection: str | None = None, job_id: str | None = None) -> list[DocumentInfo]:
    with _lock:
        _load()
        docs = [d for d in _docs.values() if (collection is None or d.collection == collection)
                and (job_id is None or d.job_id == job_id)]
        return sorted(docs, key=lambda d: d.created_at, reverse=True)


def delete(doc_id: str) -> Optional[DocumentInfo]:
    with _lock:
        _load()
        doc = _docs.pop(doc_id, None)
        if doc is not None:
            job = _jobs.get(doc.job_id)
            if job and doc_id in job["doc_ids"]:
                job["doc_ids"].remove(doc_id)
                if not job["doc_ids"]:
                    _jobs.pop(doc.job_id, None)
            _save()
        return doc


def collections() -> list[str]:
    with _lock:
        _load()
        return sorted({d.collection for d in _docs.values()})
