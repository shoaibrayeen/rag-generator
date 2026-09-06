"""
Record an animated walkthrough of the UI as documentation/demo.gif.

    pip install -r requirements-dev.txt && python -m playwright install chromium
    rag serve   (or docker compose up)   # the API must be running
    python scripts/make_demo_gif.py [--api http://localhost:8000] [--out documentation/demo.gif]

The script drives the real pages headlessly: uploads the sample corpus as one job (including a
duplicate and an unsupported file), shows the job → documents filter, asks a scoped question,
visits /jobs and /documents, opens a show page, asks in the chat, clicks a page citation to
highlight the passage, and finally shows a DUPLICATE document's not-applicable page. Every frame
gets a caption; clicks are marked with a red ring. Uses a throw-away collection `demo` and deletes
it afterwards. Re-run whenever the UI flow changes (see CLAUDE.md).
"""
from __future__ import annotations

import argparse
import io
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
COLLECTION = "demo"
VIEWPORT = {"width": 1280, "height": 820}
OUT_WIDTH = 1000

frames: list[tuple[Image.Image, int]] = []  # (image, duration ms)


def font(size: int):
    for name in ("Helvetica.ttc", "Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def snap(page, caption: str, ms: int = 2600, mark=None, step=None) -> None:
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    if mark:
        x, y = mark
        for r, w in ((26, 5), (14, 4)):
            draw.ellipse((x - r, y - r, x + r, y + r), outline=(220, 38, 38, 230), width=w)
    f, fs = font(24), font(17)
    lines = wrap(draw, caption, f, img.width - 48)
    bar_h = 36 + 30 * len(lines)
    draw.rectangle((0, img.height - bar_h, img.width, img.height), fill=(17, 24, 39, 232))
    if step:
        draw.text((24, img.height - bar_h + 8), step, font=fs, fill=(147, 197, 253))
    for i, line in enumerate(lines):
        draw.text((24, img.height - bar_h + 30 + 30 * i), line, font=f, fill=(255, 255, 255))
    frames.append((img, ms))
    print(f"  frame {len(frames):2d}: {caption[:80]}")


def wrap(draw, text: str, f, max_width: int) -> list[str]:
    """Greedy word wrap using the real font metrics (max 3 lines)."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=f) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:3]


def center(locator):
    b = locator.bounding_box()
    return (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2) if b else None


def wait_job(api: str, job_id: str, timeout: int = 180) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = httpx.get(f"{api}/api/jobs/{job_id}", timeout=30).json()
        if j["status"] in ("COMPLETED", "FAILED"):
            return j
        time.sleep(0.5)
    raise SystemExit("job did not finish in time")


def record(api: str) -> None:
    from playwright.sync_api import sync_playwright

    httpx.delete(f"{api}/api/collections/{COLLECTION}", timeout=30)
    junk = ROOT / "samples" / "_unsupported.xyz"
    junk.write_text("binary junk for the demo")
    files = [SAMPLES / "handbook.md", SAMPLES / "warranty.pdf", SAMPLES / "robot_catalog.csv",
             SAMPLES / "release_notes.html", SAMPLES / "onboarding.docx", SAMPLES / "handbook.md", junk]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)

            # ---------------- 1. home: upload as one job
            page.goto(f"{api}/?collection={COLLECTION}")
            page.wait_for_selector("#exts")
            page.fill("#collection", COLLECTION)
            page.dispatch_event("#collection", "change")
            snap(page, "Home page: upload documents, follow jobs, ask questions", step="1 / 6 · Home", ms=2200)
            page.set_input_files("#files", [str(f) for f in files])
            page.wait_for_timeout(300)
            snap(page, "7 files chosen — including a duplicate and an unsupported .xyz — uploaded as ONE job",
                 mark=center(page.locator("#uploadBtn")), step="1 / 6 · Home")
            page.click("#uploadBtn")
            page.wait_for_selector("#jobs tr.clickable")
            page.wait_for_timeout(700)
            snap(page, "Job accepted instantly (job id shown); files are QUEUED / PROCESSING in the background",
                 step="1 / 6 · Home", ms=2600)
            job_id = httpx.get(f"{api}/api/jobs?collection={COLLECTION}&size=1", timeout=30).json()["items"][0]["job_id"]
            job = wait_job(api, job_id)
            page.wait_for_timeout(1800)
            snap(page, f"Job COMPLETED: {job['reason']} — Documents panel shows pages, status and reason per file",
                 step="1 / 6 · Home", ms=3200)

            # ---------------- 2. job -> documents filter, scoped ask
            row = page.locator("#jobs tr.clickable").first
            snap(page, "Click a job row to filter the Documents panel to that job", mark=center(row), step="2 / 6 · Home")
            row.click()
            page.wait_for_selector("#jobFilter .chip")
            page.wait_for_timeout(600)
            snap(page, "Documents filtered to the job (chip shows the active filter); DUPLICATE links to its original",
                 step="2 / 6 · Home")
            cb = page.locator("#jobs input[type=checkbox]").first
            cb.check()
            page.fill("#question", "How long is the warranty on Titan series industrial arms?")
            page.wait_for_timeout(300)
            snap(page, "Tick the job to narrow the scope, type a question, Ask", mark=center(page.locator("#askBtn")), step="2 / 6 · Home")
            page.click("#askBtn")
            page.wait_for_selector("#cites b", timeout=120000)
            page.wait_for_timeout(800)
            snap(page, "Grounded answer with [n] citations, a Citations list and every retrieved chunk (dense + BM25 -> RRF)",
                 step="2 / 6 · Home", ms=3600)

            # ---------------- 3. /jobs
            page.goto(f"{api}/jobs?collection={COLLECTION}")
            page.wait_for_selector("#rows tr.clickable")
            page.wait_for_timeout(500)
            snap(page, "/jobs — dedicated job listing, 20 per page: documents, pages, chunks, status, per-status counts, reason",
                 step="3 / 6 · Jobs page", ms=3000)

            # ---------------- 4. /documents
            page.goto(f"{api}/documents?collection={COLLECTION}")
            page.wait_for_selector("#rows tr.clickable")
            page.wait_for_timeout(500)
            snap(page, "/documents — every uploaded file, 20 per page, with pages, chunks, status, reason; filters in the URL",
                 step="4 / 6 · Documents page", ms=3000)
            page.select_option("#status", "DUPLICATE")
            page.wait_for_timeout(900)
            snap(page, "Filter by status (here DUPLICATE); click a job id to see that job's files, click a row for the show page",
                 step="4 / 6 · Documents page", ms=2800)

            # ---------------- 5. show page: viewer + chat + highlight
            docs = httpx.get(f"{api}/api/documents?collection={COLLECTION}&size=50", timeout=30).json()["items"]
            warranty = next(d for d in docs if d["filename"] == "warranty.pdf")
            dup = next(d for d in docs if d["status"] == "DUPLICATE")
            page.goto(f"{api}/documents/{warranty['doc_id']}")
            page.wait_for_selector(".vpage")
            page.wait_for_timeout(600)
            snap(page, "Show page: left 55% = Text view of the indexed pages (Original file / Details tabs); right 45% = chat for THIS document only",
                 step="5 / 6 · Show page", ms=3400)
            page.fill("#q", "How long is the warranty on Titan series industrial arms, and what is excluded?")
            snap(page, "Ask in the chat — the question is sent with doc_ids=[this document]", mark=center(page.locator("#send")), step="5 / 6 · Show page", ms=2200)
            page.click("#send")
            page.wait_for_selector(".msg.bot cite.c", timeout=120000)
            page.wait_for_timeout(900)
            snap(page, "Answer arrives with PAGE-NUMBER chips; the first cited page is focused and its passages highlighted in light blue",
                 step="5 / 6 · Show page", ms=3800)
            chips = page.locator(".msg.bot cite.c")
            last = chips.nth(chips.count() - 1)
            snap(page, "Click any page chip ...", mark=center(last), step="5 / 6 · Show page", ms=1800)
            last.click()
            page.wait_for_timeout(1200)
            snap(page, "... the viewer scrolls to that page, outlines it and highlights the cited passage (clicked page darker blue)",
                 step="5 / 6 · Show page", ms=3800)

            # ---------------- 6. duplicate show page
            page.goto(f"{api}/documents/{dup['doc_id']}")
            page.wait_for_selector(".banner")
            page.wait_for_timeout(500)
            snap(page, "A DUPLICATE document's show page: viewer/chat not applicable, with the ORIGINAL document's show-page URL",
                 step="6 / 6 · Not applicable", ms=3600)
            browser.close()
    finally:
        junk.unlink(missing_ok=True)
        httpx.delete(f"{api}/api/collections/{COLLECTION}", timeout=30)


def save_gif(out: Path) -> None:
    imgs = []
    for img, _ in frames:
        w = OUT_WIDTH
        h = round(img.height * w / img.width)
        imgs.append(img.resize((w, h), Image.LANCZOS).quantize(colors=160, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG))
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=[d for _, d in frames], loop=0, optimize=False)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB, {len(imgs)} frames)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--out", type=Path, default=ROOT / "documentation" / "demo.gif")
    a = p.parse_args()
    httpx.get(f"{a.api}/api/health", timeout=30).raise_for_status()
    print("recording…")
    record(a.api.rstrip("/"))
    save_gif(a.out)


if __name__ == "__main__":
    main()
