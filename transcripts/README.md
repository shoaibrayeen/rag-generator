# Agent transcripts

This folder holds the complete Claude Code transcripts for the sessions in which this
project was built, as required by the assessment brief.

How a transcript gets here: at the end of a Claude Code session run

    /export transcripts/<YYYY-MM-DD>-rag-generator-<topic>.md

or, equivalently, `python scripts/export_transcript.py --session <session-id>` which converts the
session log under `~/.claude/projects/<project>/` to the same Markdown structure.

(or use the desktop app's export action and save the file into this folder). Transcripts
are plain Markdown and are committed as-is; nothing in them is edited after export.

| File | Session |
|---|---|
| `2026-09-06-rag-generator-build.md` | Planning and full implementation of the RAG Generator v0.1.0 → v0.4.0. Generated from the Claude Code session log by `scripts/export_transcript.py` (the `/export`-equivalent); re-run `/export transcripts/2026-09-06-rag-generator-build.md` from the Claude Code terminal to refresh it with the final messages of the session. |
