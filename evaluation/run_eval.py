"""
Evaluate the running RAG API with Claude as judge.

    python -m evaluation.run_eval --dataset evaluation/dataset.example.jsonl [--collection default]

Dataset rows (JSONL): {"question": "...", "reference_answer": "...", "collection": "..."}.
Writes evaluation/reports/<timestamp>.json and .md, prints a summary table, and exits
non-zero when mean faithfulness is below --min-faithfulness (default EVAL_MIN_FAITHFULNESS).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from evaluation.judge import judge

REPORTS = Path(__file__).parent / "reports"


def load_dataset(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    if not rows:
        raise SystemExit(f"no rows in {path}")
    return rows


def run(dataset: Path, api: str, collection: str | None, min_faith: float, limit: int | None) -> int:
    rows = load_dataset(dataset)[:limit]
    results = []
    with httpx.Client(base_url=api.rstrip("/"), timeout=180) as c:
        for i, row in enumerate(rows, start=1):
            col = row.get("collection") or collection or settings.DEFAULT_COLLECTION
            print(f"[{i}/{len(rows)}] {row['question'][:70]}", flush=True)
            r = c.post("/api/ask", json={"question": row["question"], "collection": col})
            if r.status_code >= 400:
                print(f"   ask failed: HTTP {r.status_code} {r.text[:200]}")
                results.append({**row, "collection": col, "error": r.text[:500]})
                continue
            res = r.json()
            verdict = judge(row["question"], [s["text"] for s in res["sources"]], res["answer"],
                            row.get("reference_answer"))
            print(f"   faith={verdict.faithfulness} rel={verdict.answer_relevance} "
                  f"ctx={verdict.context_relevance} corr={verdict.correctness} "
                  f"halluc={verdict.hallucinated}")
            results.append({**row, "collection": col, "answer": res["answer"],
                            "sources": [{"source": s["source"], "page": s["page"]} for s in res["sources"]],
                            "latency_ms": res["latency_ms"], "verdict": verdict.model_dump()})

    scored = [r for r in results if "verdict" in r]
    summary = {}
    for key in ("faithfulness", "answer_relevance", "context_relevance", "correctness"):
        vals = [r["verdict"][key] for r in scored if r["verdict"].get(key) is not None]
        summary[key] = round(statistics.mean(vals), 2) if vals else None
    summary["hallucination_rate"] = (round(sum(r["verdict"]["hallucinated"] for r in scored) / len(scored), 2)
                                     if scored else None)
    summary["n"] = len(results)
    summary["n_scored"] = len(scored)
    summary["n_errors"] = len(results) - len(scored)

    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = {"generated_at": stamp, "api": api, "dataset": str(dataset), "judge_model": settings.JUDGE_MODEL,
              "llm_model": scored[0].get("model") if scored else None, "summary": summary, "results": results}
    (REPORTS / f"{stamp}.json").write_text(json.dumps(report, indent=2))
    (REPORTS / f"{stamp}.md").write_text(_markdown(report))

    print("\n== Summary ==")
    for k, v in summary.items():
        print(f"{k:20} {v}")
    print(f"report: {REPORTS / stamp}.md")

    faith = summary["faithfulness"]
    if faith is None or faith < min_faith:
        print(f"FAIL: mean faithfulness {faith} < {min_faith}")
        return 1
    return 0


def _markdown(report: dict) -> str:
    s = report["summary"]
    out = [f"# RAG evaluation {report['generated_at']}", "",
           f"- API: `{report['api']}`  ", f"- Dataset: `{report['dataset']}`  ",
           f"- Judge: `{report['judge_model']}`  ", "",
           "| metric | value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s.items()]
    out += ["", "| # | question | faith | rel | ctx | corr | halluc | answer |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(report["results"], start=1):
        if "verdict" not in r:
            out.append(f"| {i} | {r['question']} | – | – | – | – | – | ERROR: {r.get('error','')[:80]} |")
            continue
        v = r["verdict"]
        ans = r["answer"].replace("\n", " ").replace("|", "\\|")[:160]
        out.append(f"| {i} | {r['question']} | {v['faithfulness']} | {v['answer_relevance']} | "
                   f"{v['context_relevance']} | {v.get('correctness') or '–'} | {'yes' if v['hallucinated'] else 'no'} | {ans} |")
    out += ["", "## Rationales", ""]
    for i, r in enumerate(report["results"], start=1):
        if "verdict" in r:
            out.append(f"{i}. {r['verdict']['rationale']}")
    return "\n".join(out) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=Path("evaluation/dataset.example.jsonl"))
    p.add_argument("--api", default=settings.RAG_API_URL)
    p.add_argument("--collection", default=None, help="override collection for all rows")
    p.add_argument("--min-faithfulness", type=float, default=settings.EVAL_MIN_FAITHFULNESS)
    p.add_argument("--limit", type=int, default=None)
    a = p.parse_args()
    sys.exit(run(a.dataset, a.api, a.collection, a.min_faithfulness, a.limit))


if __name__ == "__main__":
    main()
