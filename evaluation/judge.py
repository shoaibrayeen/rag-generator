"""
Claude-as-judge for RAG answers.

Uses the official `anthropic` SDK with structured outputs (`messages.parse`) so every
verdict is a validated pydantic object. Model and key come from settings
(JUDGE_MODEL, ANTHROPIC_API_KEY). Thinking is left at the model default (adaptive).
"""
from __future__ import annotations

from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from app.config import settings

JUDGE_SYSTEM = """You are a strict evaluator of a retrieval-augmented QA system.
You are given a QUESTION, the CONTEXT passages the system retrieved, the system's ANSWER,
and optionally a REFERENCE answer written by a human.

Score each dimension from 1 (worst) to 5 (best):
- faithfulness: every claim in ANSWER is supported by CONTEXT. Saying "not found in the
  documents" when the context truly lacks the answer is fully faithful (5).
- answer_relevance: ANSWER directly addresses QUESTION without padding or evasion.
- context_relevance: the retrieved CONTEXT contains what is needed to answer QUESTION.
- correctness: ANSWER agrees with REFERENCE. Use null when no REFERENCE is given.
Set hallucinated=true if ANSWER states anything not supported by CONTEXT.
Keep the rationale to two or three sentences."""


class JudgeVerdict(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    answer_relevance: int = Field(ge=1, le=5)
    context_relevance: int = Field(ge=1, le=5)
    correctness: Optional[int] = Field(default=None, ge=1, le=5)
    hallucinated: bool
    rationale: str


class GeneratedQA(BaseModel):
    question: str
    reference_answer: str


class GeneratedQASet(BaseModel):
    items: list[GeneratedQA]


def _client() -> anthropic.Anthropic:
    if not settings.ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set in .env; the judge needs it.")
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def judge(question: str, contexts: list[str], answer: str,
          reference: str | None = None) -> JudgeVerdict:
    ctx = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts, start=1)) or "(no context retrieved)"
    user = f"QUESTION:\n{question}\n\nCONTEXT:\n{ctx}\n\nANSWER:\n{answer}\n"
    if reference:
        user += f"\nREFERENCE:\n{reference}\n"
    resp = _client().messages.parse(
        model=settings.JUDGE_MODEL,
        max_tokens=settings.JUDGE_MAX_TOKENS,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=JudgeVerdict,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("judge model refused the request")
    return resp.parsed_output


def generate_questions(chunk_text: str, source: str, n: int) -> list[GeneratedQA]:
    prompt = (f"Below is a passage from the document '{source}'. Write {n} distinct, specific "
              f"questions that can be answered ONLY from this passage, each with a short reference "
              f"answer quoting the relevant facts.\n\nPASSAGE:\n{chunk_text}")
    resp = _client().messages.parse(
        model=settings.JUDGE_MODEL,
        max_tokens=settings.JUDGE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        output_format=GeneratedQASet,
    )
    if resp.stop_reason == "refusal":
        return []
    return resp.parsed_output.items[:n]
