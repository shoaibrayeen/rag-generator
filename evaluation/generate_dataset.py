"""
Synthesize an eval dataset from whatever is currently indexed, using Claude.

    python -m evaluation.generate_dataset --collection default --chunks 10 --per-chunk 2 \
        --out evaluation/dataset.generated.jsonl

Samples chunks evenly across the collection via GET /api/chunks and asks the judge model
to write questions answerable from each chunk. This is how the eval stays useful when
the document set changes without any code changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from app.config import settings
from evaluation.judge import generate_questions


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default=settings.RAG_API_URL)
    p.add_argument("--collection", default=settings.DEFAULT_COLLECTION)
    p.add_argument("--chunks", type=int, default=10, help="how many chunks to sample")
    p.add_argument("--per-chunk", type=int, default=2)
    p.add_argument("--out", type=Path, default=Path("evaluation/dataset.generated.jsonl"))
    a = p.parse_args()

    with httpx.Client(base_url=a.api.rstrip("/"), timeout=60) as c:
        chunks = c.get("/api/chunks", params={"collection": a.collection, "limit": 1000}).json()
    if not chunks:
        raise SystemExit(f"collection '{a.collection}' has no chunks")
    step = max(1, len(chunks) // a.chunks)
    sample = chunks[::step][: a.chunks]

    rows = []
    for ch in sample:
        for qa in generate_questions(ch["text"], ch["source"], a.per_chunk):
            rows.append({"question": qa.question, "reference_answer": qa.reference_answer,
                         "collection": a.collection, "source": ch["source"], "page": ch["page"]})
            print(f"- {qa.question}")
    a.out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"wrote {len(rows)} rows to {a.out}")


if __name__ == "__main__":
    main()
