"""
Mock OpenAI-compatible LLM for offline demos and tests. NOT a real model.

    python scripts/mock_llm.py            # serves http://localhost:8001/v1/chat/completions

Point .env at it:  LLM_API_URL=http://localhost:8001/v1  LLM_API_KEY=mock  LLM_MODEL=mock-extractive
It answers extractively: it picks the CONTEXT block that best overlaps the question and
returns its most relevant sentences with a [n] citation, or the "not found" sentence.
Swap the three LLM_* lines for a real endpoint to get genuine generated answers.
"""
from __future__ import annotations

import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="mock-llm")
NOT_FOUND = "I could not find this in the provided documents."
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "is", "what", "how", "does", "do", "for", "to", "in", "on", "and",
         "are", "many", "much", "which", "who", "when", "long", "with", "per", "at"}


def _tokens(s: str) -> set[str]:
    return {t for t in _WORD.findall(s.lower()) if t not in _STOP}


def _answer(user_content: str) -> str:
    m = re.search(r"CONTEXT:\n(.*)\n\nQUESTION: (.*?)\n\nANSWER", user_content, re.S)
    if not m:
        return NOT_FOUND
    context, question = m.group(1), m.group(2)
    blocks = re.findall(r"\[(\d+)\] \((.*?)\)\n(.*?)(?=\n\n\[\d+\] \(|\Z)", context, re.S)
    q = _tokens(question)
    best, best_score, best_sents = None, 0.0, []
    for n, _loc, text in blocks:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]
        scored = sorted(((len(q & _tokens(s)), i, s) for i, s in enumerate(sents)), reverse=True)
        top = [s for sc, _, s in scored[:2] if sc > 0]
        score = sum(sc for sc, _, _ in scored[:2])
        if score > best_score:
            best, best_score, best_sents = n, score, [s for _, _, s in sorted((i, i, s) for sc, i, s in scored[:2] if sc > 0)]
    if best is None or best_score < 2:
        return NOT_FOUND
    return " ".join(best_sents) + f" [{best}]"


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    user = next((m["content"] for m in reversed(body.get("messages", [])) if m["role"] == "user"), "")
    text = _answer(user)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
        "model": body.get("model", "mock-extractive"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": len(text) // 4,
                  "total_tokens": (len(user) + len(text)) // 4},
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
