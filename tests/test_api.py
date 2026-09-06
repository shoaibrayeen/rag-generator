"""End-to-end API tests with the LLM mocked (embeddings + Chroma + BM25 are real)."""
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import TERMINAL_STATUSES

DATA = settings.DATA_DIR
JOB_TERMINAL = ("COMPLETED", "FAILED")


@pytest.fixture(scope="module")
def client():
    shutil.rmtree(DATA, ignore_errors=True)
    from app.main import app

    with TestClient(app) as c:
        yield c
    shutil.rmtree(DATA, ignore_errors=True)


def _wait_job(client, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in JOB_TERMINAL:
            return j
        time.sleep(0.2)
    raise AssertionError("timed out waiting for job")


def _upload(client, files, collection):
    r = client.post("/api/documents", files=[("files", (n, b)) for n, b in files], data={"collection": collection})
    assert r.status_code == 202, r.text
    return r.json()


POLICY = (b"# Refund policy\nCustomers may request a refund within 30 days of purchase.\n\n"
          b"# Shipping\nOrders ship within 2 business days.\n")
SECURITY = b"Passwords must be rotated every 180 days and be at least 14 characters long."


def test_collection_name_validation(client):
    r = client.post("/api/documents", files=[("files", ("a.txt", b"x"))], data={"collection": "ab"})
    assert r.status_code == 400 and "3-63" in r.json()["detail"]


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and ".pdf" in h["supported_extensions"]
    assert "LLM_API_KEY" not in h["settings"]
    assert set(h["statuses"]) == {"QUEUED", "DUPLICATE", "PROCESSING", "COMPLETED", "FAILED",
                                  "EMPTY_FILE", "EXTRACTION_NOT_SUPPORTED", "UPLOAD_FAILED"}


def test_job_lifecycle_listing_and_filter(client):
    job = _upload(client, [("policy.md", POLICY), ("security.txt", SECURITY), ("junk.xyz", b"?")], "col1")
    assert job["files"] == 3 and job["status"] in ("QUEUED", "PROCESSING")
    assert {d["status"] for d in job["documents"]} <= {"QUEUED", "EXTRACTION_NOT_SUPPORTED"}
    assert all(d["job_id"] == job["job_id"] for d in job["documents"])

    done = _wait_job(client, job["job_id"])
    assert done["status"] == "COMPLETED", done
    assert done["counts"] == {"COMPLETED": 2, "EXTRACTION_NOT_SUPPORTED": 1}
    assert done["reason"].startswith("partially completed")

    page = client.get("/api/jobs", params={"collection": "col1"}).json()
    assert page["page"] == 0 and page["size"] == 5 and page["total"] == 1 and page["pages"] == 1
    assert page["has_next"] is False and page["has_prev"] is False
    jobs = page["items"]
    assert [j["job_id"] for j in jobs] == [job["job_id"]]
    assert {"job_id", "files", "status", "reason"} <= set(jobs[0])

    # job -> document listing filter
    docs = client.get("/api/documents", params={"job_id": job["job_id"]}).json()["items"]
    assert {d["filename"] for d in docs} == {"policy.md", "security.txt", "junk.xyz"}
    empty = client.get("/api/documents", params={"job_id": "nope"}).json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["pages"] == 0
    assert client.get("/api/jobs/nope").status_code == 404

    # hash is computed at upload time and kept on every record with content
    assert all(d["sha256"] for d in docs if d["status"] != "EMPTY_FILE")

    # duplicate in a second job -> job COMPLETED (nothing new indexed), doc DUPLICATE linking original
    job2 = _upload(client, [("policy-copy.md", POLICY)], "col1")
    done2 = _wait_job(client, job2["job_id"])
    dup = done2["documents"][0]
    original = next(d for d in docs if d["filename"] == "policy.md")
    assert dup["status"] == "DUPLICATE" and dup["duplicate_of"] == original["doc_id"]
    assert dup["sha256"] == original["sha256"]
    assert done2["status"] == "COMPLETED" and done2["counts"] == {"DUPLICATE": 1}

    # a job whose only file fails -> FAILED
    job3 = _upload(client, [("broken.pdf", b"not a pdf")], "col1")
    done3 = _wait_job(client, job3["job_id"])
    assert done3["status"] == "FAILED" and done3["reason"].startswith("no file could be indexed")
    reason = done3["documents"][0]["reason"]
    assert reason.startswith("PROCESSING FAILED: Could not read 'broken.pdf': the pdf reader failed (FileDataError")
    assert "/Users" not in reason and "tests/_data" not in reason  # no absolute paths leak into the reason


def test_ask_scoped_by_docs_and_jobs_with_citations(client, monkeypatch):
    seen = {}

    def fake_chat(messages, **kw):
        seen["prompt"] = messages[1]["content"]
        # cites source 1 and a bogus 9 that must be removed and reported
        return "The refund window is 30 days [1]. Bogus claim [9]."

    monkeypatch.setattr("app.rag.llm.chat", fake_chat)

    jp = _upload(client, [("policy.md", POLICY)], "col2")
    js = _upload(client, [("security.txt", SECURITY)], "col2")
    _wait_job(client, jp["job_id"])
    _wait_job(client, js["job_id"])
    policy_id = jp["documents"][0]["doc_id"]
    security_id = js["documents"][0]["doc_id"]

    # whole collection: both documents can appear
    r = client.post("/api/ask", json={"question": "How long is the refund window?", "collection": "col2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == {"collection": "col2", "doc_ids": [], "job_ids": [], "restricted": False}
    assert body["answer"] == "The refund window is 30 days [1]. Bogus claim."  # only the bogus [9] marker is removed
    assert [c["n"] for c in body["citations"]] == [1] and body["unresolved_citations"] == [9]
    assert body["citations"][0]["source"] == "policy.md" and body["citations"][0]["page"] == 1
    assert {s["doc_id"] for s in body["sources"]} == {policy_id, security_id}

    # chunk store carries job_id / collection and can be filtered by either
    chunks = client.get("/api/chunks", params={"collection": "col2", "job_id": jp["job_id"]}).json()
    assert chunks and all(c["job_id"] == jp["job_id"] and c["doc_id"] == policy_id and c["collection"] == "col2" for c in chunks)
    assert client.get("/api/chunks", params={"collection": "col2", "doc_id": security_id}).json()[0]["source"] == "security.txt"
    assert client.get("/api/chunks", params={"collection": "col2", "job_id": "nope"}).json() == []

    # scoped to the security document only: no policy chunk may be retrieved
    r = client.post("/api/ask", json={"question": "How long is the refund window?", "doc_ids": [security_id]})
    body = r.json()
    assert body["scope"]["restricted"] and body["scope"]["doc_ids"] == [security_id]
    assert {s["doc_id"] for s in body["sources"]} == {security_id}
    assert "Passwords" in seen["prompt"] and "refund" not in seen["prompt"].lower().split("question:")[0]

    # scoped by job id -> resolves to that job's COMPLETED documents
    r = client.post("/api/ask", json={"question": "refund?", "job_ids": [jp["job_id"]]})
    body = r.json()
    assert body["scope"]["job_ids"] == [jp["job_id"]] and body["scope"]["doc_ids"] == [policy_id]
    assert {s["doc_id"] for s in body["sources"]} == {policy_id}

    # docs + jobs together are unioned
    r = client.post("/api/ask", json={"question": "refund?", "job_ids": [js["job_id"]], "doc_ids": [policy_id]})
    assert set(r.json()["scope"]["doc_ids"]) == {policy_id, security_id}

    # validation
    assert client.post("/api/ask", json={"question": "q", "doc_ids": ["missing"]}).status_code == 404
    assert client.post("/api/ask", json={"question": "q", "job_ids": ["missing"]}).status_code == 404
    r = client.post("/api/ask", json={"question": "q", "doc_ids": [policy_id], "collection": "other"})
    assert r.status_code == 400 and "does not match" in r.json()["detail"]
    bad = _upload(client, [("x.exe", b"MZ")], "col2")["documents"][0]["doc_id"]
    r = client.post("/api/ask", json={"question": "q", "doc_ids": [bad]})
    assert r.status_code == 409 and "EXTRACTION_NOT_SUPPORTED" in r.json()["detail"]
    other = _upload(client, [("policy.md", POLICY)], "col3")
    _wait_job(client, other["job_id"])
    r = client.post("/api/ask", json={"question": "q", "doc_ids": [policy_id, other["documents"][0]["doc_id"]]})
    assert r.status_code == 400 and "several collections" in r.json()["detail"]

    # a duplicate id resolves to its original
    dup = _upload(client, [("copy.md", POLICY)], "col2")["documents"][0]
    r = client.post("/api/ask", json={"question": "refund?", "doc_ids": [dup["doc_id"]]})
    assert r.status_code == 200 and r.json()["scope"]["doc_ids"] == [policy_id]

    # delete + empty scope message
    assert client.delete(f"/api/documents/{policy_id}").status_code == 204
    r = client.post("/api/ask", json={"question": "anything", "collection": "col2", "doc_ids": [security_id]})
    assert r.status_code == 200 and r.json()["sources"]
    client.delete("/api/collections/col2")
    client.delete("/api/collections/t3")
    r = client.post("/api/ask", json={"question": "anything", "collection": "col2"})
    assert r.json()["sources"] == [] and "could not find" in r.json()["answer"]
    assert client.get("/api/jobs", params={"collection": "col2"}).json()["items"] == []


def test_rejections_become_records(client):
    job = _upload(client, [("x.exe", b"MZ"), ("empty.txt", b"")], "rej")
    by_name = {d["filename"]: d for d in job["documents"]}
    assert by_name["x.exe"]["status"] == "EXTRACTION_NOT_SUPPORTED" and ".exe" in by_name["x.exe"]["reason"]
    assert by_name["empty.txt"]["status"] == "EMPTY_FILE"
    assert job["status"] == "FAILED"
    client.delete("/api/collections/rej")


def test_llm_error_is_502(client, monkeypatch):
    from app.llm.client import LLMError

    def boom(messages, **kw):
        raise LLMError("bad key")

    monkeypatch.setattr("app.rag.llm.chat", boom)
    job = _upload(client, [("n.txt", b"The capital of Freedonia is Marxburg.")], "err")
    _wait_job(client, job["job_id"])
    r = client.post("/api/ask", json={"question": "capital of Freedonia?", "collection": "err"})
    assert r.status_code == 502 and "bad key" in r.json()["detail"]
    client.delete("/api/collections/err")


def test_pagination_defaults_and_navigation(client):
    # 7 single-file jobs -> 7 jobs and 7 documents; default page size is 5, page index is 0-based
    for i in range(7):
        _upload(client, [(f"f{i}.txt", f"document number {i} about pagination".encode())], "pag")
    first = client.get("/api/jobs", params={"collection": "pag"}).json()
    assert first["page"] == 0 and first["size"] == 5 and first["total"] == 7 and first["pages"] == 2
    assert len(first["items"]) == 5 and first["has_next"] and not first["has_prev"]
    second = client.get("/api/jobs", params={"collection": "pag", "page": 1}).json()
    assert len(second["items"]) == 2 and second["has_prev"] and not second["has_next"]
    assert {j["job_id"] for j in first["items"]}.isdisjoint({j["job_id"] for j in second["items"]})
    # newest first: page 0 holds the most recent uploads
    assert first["items"][0]["created_at"] >= second["items"][-1]["created_at"]
    beyond = client.get("/api/jobs", params={"collection": "pag", "page": 5}).json()
    assert beyond["items"] == [] and beyond["total"] == 7

    docs = client.get("/api/documents", params={"collection": "pag", "size": 3, "page": 2}).json()
    assert docs["pages"] == 3 and len(docs["items"]) == 1 and docs["has_prev"] and not docs["has_next"]
    assert client.get("/api/documents", params={"collection": "pag", "size": 0}).status_code == 422
    assert client.get("/api/documents", params={"collection": "pag", "page": -1}).status_code == 422
    client.delete("/api/collections/pag")


RTF_BYTES = (rb"{\rtf1\ansi\deff0{\fonttbl{\f0 Arial;}}\pard Credit Risk Assessment dated 17 December 2025."
             rb"\par The borrower rating is BBB and the exposure limit is EUR 2 million.\par}")


def test_rtf_in_docx_clothing_is_indexed_and_retry_works(client, monkeypatch):
    job = _upload(client, [("2P CRA dated 17_12_2025.docx", RTF_BYTES)], "rtf")
    done = _wait_job(client, job["job_id"])
    doc = done["documents"][0]
    assert doc["status"] == "COMPLETED", doc
    assert "content is RTF" in doc["reason"] and doc["pages"] == 1

    # retry: only FAILED documents; simulate a failure then retry from the stored file
    r = client.post(f"/api/documents/{doc['doc_id']}/retry")
    assert r.status_code == 409
    job2 = _upload(client, [("old.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)], "rtf")
    failed = _wait_job(client, job2["job_id"])["documents"][0]
    assert failed["status"] == "FAILED" and "legacy binary Word" in failed["reason"]
    r = client.post(f"/api/documents/{failed['doc_id']}/retry")
    assert r.status_code == 202 and r.json()["status"] == "QUEUED"
    again = _wait_job(client, job2["job_id"])["documents"][0]
    assert again["status"] == "FAILED"  # same file, same outcome, but processed again
    assert client.post("/api/documents/nope/retry").status_code == 404
    client.delete("/api/collections/rtf")


def test_dedicated_pages_and_file_endpoint(client):
    for path in ("/", "/jobs", "/documents", "/documents/anything"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path
    assert "20" in client.get("/documents").text  # 20 per page on the dedicated listing
    job = _upload(client, [("notes.txt", b"Plain text to view inline."), ("x.exe", b"MZ")], "pages")
    _wait_job(client, job["job_id"])
    ok = next(d for d in job["documents"] if d["filename"] == "notes.txt")
    r = client.get(f"/api/documents/{ok['doc_id']}/file")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and b"inline" in r.content
    assert "inline" in r.headers["content-disposition"]
    r = client.get(f"/api/documents/{ok['doc_id']}/file", params={"download": "true"})
    assert "attachment" in r.headers["content-disposition"]
    rejected = next(d for d in job["documents"] if d["filename"] == "x.exe")
    assert client.get(f"/api/documents/{rejected['doc_id']}/file").status_code == 410  # never stored
    assert client.get("/api/documents/nope/file").status_code == 404
    client.delete("/api/collections/pages")
