"""
Render every Markdown document to a browsable HTML twin.

    python scripts/build_docs.py

Inputs : documentation/*.md  and  README.md (→ documentation/readme.html)
Outputs: documentation/<same-name>.html, styled like the rest of the documentation set.
Run this after editing any Markdown file (see CLAUDE.md); commit both the .md and the .html.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "documentation"

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAG Generator · {title}</title>
<style>
  body{{margin:0;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#1c1f26;background:#f6f7f9}}
  main{{max-width:960px;margin:0 auto;padding:32px 24px}}
  article{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:26px 30px}}
  h1{{font-size:26px;margin:0 0 12px}} h2{{font-size:20px;margin:28px 0 8px;padding-top:12px;border-top:1px solid #eef0f3}} h3{{font-size:16px;margin:18px 0 6px}}
  a{{color:#2563eb;text-decoration:none}} .nav{{font-size:13px;margin-bottom:16px}}
  pre{{background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:12px;overflow:auto;font:12.5px/1.5 ui-monospace,Menlo,monospace}}
  code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font:12.5px ui-monospace,Menlo,monospace}} pre code{{background:none;padding:0}}
  table{{border-collapse:collapse;width:100%;font-size:13.5px;margin:10px 0}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #e5e7eb;vertical-align:top}} th{{color:#6b7280}}
  .src{{color:#6b7280;font-size:12px;margin-top:18px}}
</style></head>
<body><main>
<div class="nav"><a href="/">← App</a> · <a href="/documentation/flow.html">How it works</a> · <a href="/documentation/architecture.html">Architecture</a> · <a href="/documentation/changelog.html">Changelog</a> · <a href="/documentation/readme.html">README</a> · <a href="/docs">API docs</a></div>
<article>
{body}
<p class="src">Generated from <code>{source}</code> by <code>scripts/build_docs.py</code>. Edit the Markdown, then re-run the script.</p>
</article>
</main></body></html>
"""


def render(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8")
    m = re.search(r"^# (.+)$", text, re.M)
    title = m.group(1).strip() if m else src.stem.title()
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc"])
    # Make relative links to sibling .md files point at their .html twins.
    body = re.sub(r'href="(?!https?://)([^"#]+)\.md(#[^"]*)?"', r'href="\1.html\2"', body)
    body = body.replace('href="documentation/', 'href="/documentation/')
    dst.write_text(TEMPLATE.format(title=html.escape(title), body=body,
                                   source=src.relative_to(ROOT)), encoding="utf-8")
    print(f"{src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    for md in sorted(DOCS.glob("*.md")):
        render(md, md.with_suffix(".html"))
    render(ROOT / "README.md", DOCS / "readme.html")


if __name__ == "__main__":
    main()
