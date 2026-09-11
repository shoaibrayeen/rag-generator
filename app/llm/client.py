"""Minimal OpenAI-compatible chat/completions client (httpx). Configured only via .env."""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

log = logging.getLogger("rag.llm")


class LLMError(RuntimeError):
    """Raised with a human-readable message when the LLM endpoint cannot be used."""


def chat(messages: list[dict], *, temperature: float | None = None,
         max_tokens: int | None = None) -> str:
    if not settings.LLM_API_KEY:
        raise LLMError("LLM_API_KEY is not set. Add it to .env (see .env.example).")
    payload = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE if temperature is None else temperature,
        "max_tokens": settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
    }
    headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}", "Content-Type": "application/json"}
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    log.info("[llm] request model=%s url=%s messages=%d prompt_chars=%d", settings.LLM_MODEL,
             settings.chat_completions_url, len(messages), prompt_chars)
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=settings.LLM_TIMEOUT_S) as http:
            resp = http.post(settings.chat_completions_url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach LLM endpoint {settings.chat_completions_url}: {exc}") from exc
    if resp.status_code >= 400:
        body = resp.text[:500]
        raise LLMError(f"LLM endpoint returned HTTP {resp.status_code} for model "
                       f"'{settings.LLM_MODEL}': {body}")
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        log.info("[llm] response in %d ms chars=%d tokens=%s", (time.perf_counter() - t0) * 1000, len(text),
                 usage.get("total_tokens", "n/a"))
        return text
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {resp.text[:300]}") from exc
