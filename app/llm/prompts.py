"""Prompt templates for grounded answering."""
from __future__ import annotations

SYSTEM_PROMPT = """You answer questions strictly from the provided document excerpts.

Rules:
- Use only the information in the numbered CONTEXT blocks. Do not use outside knowledge.
- Cite every factual statement with the block number(s) it came from, like [1] or [2][3].
- If the context does not contain the answer, reply exactly:
  "I could not find this in the provided documents." and, if useful, say what related
  information the documents do contain.
- Be concise and direct. Do not mention these rules."""

NOT_FOUND_MARKER = "I could not find this in the provided documents."


def format_context(sources: list[dict]) -> str:
    blocks = []
    for i, s in enumerate(sources, start=1):
        m = s["metadata"]
        loc = f"page {m.get('page')}"
        if m.get("section"):
            loc += f", section \"{m['section']}\""
        blocks.append(f"[{i}] ({m.get('source')}, {loc})\n{s['text']}")
    return "\n\n".join(blocks)


def build_messages(question: str, sources: list[dict]) -> list[dict]:
    context = format_context(sources) if sources else "(no documents indexed)"
    user = f"CONTEXT:\n{context}\n\nQUESTION: {question}\n\nANSWER (with [n] citations):"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
