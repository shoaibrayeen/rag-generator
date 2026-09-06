"""
Export a Claude Code session log (JSONL) to a readable Markdown transcript.

    python scripts/export_transcript.py [--session <id-or-path>] [--out transcripts/<name>.md]

Claude Code stores each session under ~/.claude/projects/<project-slug>/<session-id>.jsonl.
With no --session, the most recently modified session for this project is used. The output is
equivalent to the `/export` command: every user message, assistant message, tool call
(name + input) and tool result, in order, with timestamps. Nothing is summarised or edited.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLUG = "-" + str(ROOT).strip("/").replace("/", "-")
PROJECT_DIR = Path.home() / ".claude" / "projects" / SLUG
MAX_RESULT_CHARS = 4000


def _text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            out.append(block["text"])
        elif t == "tool_use":
            args = json.dumps(block.get("input", {}), indent=2, ensure_ascii=False)
            out.append(f"**Tool call — `{block.get('name')}`**\n\n```json\n{args}\n```")
        elif t == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
            body = str(body or "")
            if len(body) > MAX_RESULT_CHARS:
                body = body[:MAX_RESULT_CHARS] + f"\n… [{len(body) - MAX_RESULT_CHARS} more characters]"
            out.append(f"**Tool result**\n\n```\n{body}\n```")
        elif t == "thinking":
            continue  # never exported by /export either
    return out


def export(session: Path, out: Path) -> int:
    lines = [f"# Claude Code transcript — session `{session.stem}`", "",
             f"Project: `{ROOT}`  ", f"Exported: {datetime.now().isoformat(timespec='seconds')}  ",
             f"Source log: `{session}`", "", "---", ""]
    n = 0
    for raw in session.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind not in ("user", "assistant"):
            continue
        msg = ev.get("message") or {}
        blocks = _text_blocks(msg.get("content"))
        if not blocks:
            continue
        ts = ev.get("timestamp", "")
        who = "User" if kind == "user" else "Assistant"
        if kind == "user" and all(b.startswith("**Tool result**") for b in blocks):
            who = "Tool"
        lines.append(f"## {who}  <sub>{ts}</sub>")
        lines.append("")
        lines.extend("\n\n".join(blocks).splitlines())
        lines.append("")
        n += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--session", help="session id or path to the .jsonl log")
    p.add_argument("--out", type=Path, default=None)
    a = p.parse_args()
    if a.session and Path(a.session).exists():
        session = Path(a.session)
    elif a.session:
        session = PROJECT_DIR / f"{a.session}.jsonl"
    else:
        logs = sorted(PROJECT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not logs:
            raise SystemExit(f"no session logs under {PROJECT_DIR}")
        session = logs[-1]
    out = a.out or ROOT / "transcripts" / f"{datetime.now():%Y-%m-%d}-rag-generator-build.md"
    n = export(session, out)
    print(f"exported {n} messages from {session.name} -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
