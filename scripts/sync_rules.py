"""
Keep the AI-agent rule files identical across tools.

Source of truth: CLAUDE.md. Targets:
  .cursor/rules/project.mdc   (Cursor; gets a small frontmatter header, alwaysApply)
  AGENTS.md                   (Codex / generic agents)

    python scripts/sync_rules.py          # write targets from CLAUDE.md
    python scripts/sync_rules.py --check  # exit 1 if any target is out of sync
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "CLAUDE.md"
CURSOR_HEADER = "---\ndescription: Project rules (synced from CLAUDE.md — edit that file, then run scripts/sync_rules.py)\nalwaysApply: true\n---\n\n"
NOTE = "<!-- Synced from CLAUDE.md by scripts/sync_rules.py. Do not edit this copy. -->\n\n"

TARGETS = {
    ROOT / ".cursor" / "rules" / "project.mdc": CURSOR_HEADER,
    ROOT / "AGENTS.md": NOTE,
}


def main() -> int:
    body = SOURCE.read_text(encoding="utf-8")
    check = "--check" in sys.argv
    drift = 0
    for path, header in TARGETS.items():
        expected = header + body
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == expected:
            continue
        if check:
            print(f"OUT OF SYNC: {path.relative_to(ROOT)}")
            drift += 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if check and not drift:
        print("rules in sync")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
