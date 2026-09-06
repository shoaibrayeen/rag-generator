"""End-to-end API test with the LLM mocked (embeddings + Chroma + BM25 are real)."""
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import TERMINAL_STATUSES

DATA = settings.DATA_DIR


@pytest.fixture(scope="module")
def client():
    shutil.rmtree(DATA, ignore_errors=True)
    from app.main import app

    with TestClient(app) as c:
        yield c
    shutil.rmtree(DATA, ignore_errors=True)


def _wait(client, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["status"] in TERMINAL_STATUSES:
            return d
        time.sleep(0.2)
    raise AssertionError("timed out waiting for job")


def _upload(client, name, data, collection):
    r = client.post("/api/documents", files=[("files", (name, data))], data={"collection": collection})
    assert r.status_code == 202, r.text
    return r.json()["documents"]


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and ".pdf" in h["supported_extensions"]
    assert "LLM_API_KEY" not in h["settings"]
    assert set(h["statuses"]) == {"QUEUED", "DUPLICATE", "PROCESSING", "COMPLETED", "FAILED",
                                  "EMPTY_FILE", "EXTRACTION_NOT_SUPPORTED", "UPLOAD_FAILED"}


def test_async_upload_ask_duplicate_delete(client, monkeypatch):
    captured = {}

    def fake_chat(messages, **kw):
        captured["messages"] = messages
        return "The refund window is 30 days [1]."

    monkeypatch.setattr("app.rag.llm.chat", fake_chat)

    content = (b"# Refund policy\nCustomers may request a refund within 30 days of purchase.\n\n"
               b"# Shipping\nOrders ship within 2 business days.\n")
    [doc] = _upload(client, "policy.md", content, "test")
    assert doc["status"] == "QUEUED" and doc["job_id"] == doc["doc_id"]
    d = _wait(client, doc["job_id"])
    assert d["status"] == "COMPLETED", d
    assert d["pages"] == 2 and d["chunks"] >= 2 and "Indexed 2 pages" in d["reason"]

    # Same bytes again -> DUPLICATE pointing at the original, never processed.
    [dup] = _upload(client, "policy-copy.md", content, "test")
    assert dup["status"] == "DUPLICATE" and dup["duplicate_of"] == doc["doc_id"]
    assert dup["duplicate_of_name"] == "policy.md" and "policy.md" in dup["reason"]
    # Same bytes in a different collection is not a duplicate.
    [other] = _upload(client, "policy.md", content, "other")
    assert other["status"] == "QUEUED"
    _wait(client, other["job_id"])
    client.delete("/api/collections/other")

    r = client.post("/api/ask", json={"question": "How long is the refund window?", "collection": "test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "30 days" in body["answer"] and body["sources"]
    top = body["sources"][0]
    assert top["source"] == "policy.md" and "refund" in top["text"].lower()
    assert top["dense_rank"] is not None or top["bm25_rank"] is not None
    assert "CONTEXT:" in captured["messages"][1]["content"]

    listing = client.get("/api/documents", params={"collection": "test"}).json()
    assert {x["status"] for x in listing} == {"COMPLETED", "DUPLICATE"}
    assert client.get("/api/documents", params={"collection": "test", "status": "DUPLICATE"}).json()[0]["doc_id"] == dup["doc_id"]

    chunks = client.get("/api/chunks", params={"collection": "test"}).json()
    assert len(chunks) == d["chunks"]

    assert client.delete(f"/api/documents/{doc['doc_id']}").status_code == 204
    assert client.delete(f"/api/documents/{dup['doc_id']}").status_code == 204
    assert client.get("/api/documents", params={"collection": "test"}).json() == []
    r = client.post("/api/ask", json={"question": "anything", "collection": "test"})
    assert r.json()["sources"] == [] and "could not find" in r.json()["answer"]


def test_rejections_become_records(client):
    docs = client.post("/api/documents", files=[("files", ("x.exe", b"MZ")), ("files", ("empty.txt", b""))],
                       data={"collection": "rej"}).json()["documents"]
    by_name = {d["filename"]: d for d in docs}
    assert by_name["x.exe"]["status"] == "EXTRACTION_NOT_SUPPORTED" and ".exe" in by_name["x.exe"]["reason"]
    assert by_name["empty.txt"]["status"] == "EMPTY_FILE"
    assert client.get(f"/api/jobs/{docs[0]['job_id']}").status_code == 200
    assert client.get("/api/jobs/nope").status_code == 404
    client.delete("/api/collections/rej")


def test_processing_failure_is_reported(client):
    # A PDF that is not really a PDF -> FAILED with a "PROCESSING FAILED: ..." reason.
    [doc] = _upload(client, "broken.pdf", b"not a pdf at all", "bad")
    d = _wait(client, doc["job_id"])
    assert d["status"] == "FAILED" and d["reason"].startswith("PROCESSING FAILED: Could not read this pdf")
    client.delete("/api/collections/bad")


def test_llm_error_is_502(client, monkeypatch):
    from app.llm.client import LLMError

    def boom(messages, **kw):
        raise LLMError("bad key")

    monkeypatch.setattr("app.rag.llm.chat", boom)
    [doc] = _upload(client, "n.txt", b"The capital of Freedonia is Marxburg.", "err")
    _wait(client, doc["job_id"])
    r = client.post("/api/ask", json={"question": "capital of Freedonia?", "collection": "err"})
    assert r.status_code == 502 and "bad key" in r.json()["detail"]
    client.delete("/api/collections/err")
