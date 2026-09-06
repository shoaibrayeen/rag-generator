"""
Turn a file into a list of `Page`s (text + page metadata).

Dispatch is driven by `app.config.SUPPORTED_EXTENSIONS`; each extractor name there
must exist in `EXTRACTORS` below. To support a new format, add an extractor
function and register it in both places.

OCR: PDF pages with little/no extractable text and image files are sent through
Tesseract (`pytesseract`). If the `tesseract` binary is missing the page is kept
with whatever text was found and tagged `extraction="ocr_unavailable"`; ingestion
never fails because of OCR.
"""
from __future__ import annotations

import csv
import io
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import SUPPORTED_EXTENSIONS, settings

log = logging.getLogger(__name__)


@dataclass
class Page:
    text: str
    page_no: int
    section: str | None = None
    extraction: str = "text"  # text | ocr | ocr_unavailable
    meta: dict = field(default_factory=dict)


class UnsupportedFormat(ValueError):
    pass


# --------------------------------------------------------------------------- OCR

_ocr_warned = False


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_image(image) -> str | None:
    """Return OCR text for a PIL image, or None when Tesseract is unavailable."""
    global _ocr_warned
    if not settings.OCR_ENABLED:
        return None
    if not ocr_available():
        if not _ocr_warned:
            log.warning("OCR requested but `tesseract` binary not found; skipping OCR. "
                        "Install it (apt install tesseract-ocr / brew install tesseract).")
            _ocr_warned = True
        return None
    import pytesseract

    try:
        return pytesseract.image_to_string(image, lang=settings.OCR_LANG)
    except Exception as exc:  # pragma: no cover - depends on host tesseract
        log.warning("OCR failed: %s", exc)
        return None


def _render_pdf_page(path: Path, index: int):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[index]
        scale = settings.OCR_DPI / 72.0
        return page.render(scale=scale).to_pil()
    finally:
        pdf.close()


# --------------------------------------------------------------------- extractors

def extract_pdf(path: Path) -> list[Page]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[Page] = []
    for i, p in enumerate(reader.pages):
        text = (p.extract_text() or "").strip()
        extraction = "text"
        if len(text) < settings.OCR_MIN_CHARS_PER_PAGE and settings.OCR_ENABLED:
            # Only render the page when Tesseract can actually consume it.
            ocr_text = _ocr_image(_render_pdf_page(path, i)) if ocr_available() else _ocr_image(None)
            if ocr_text is None:
                extraction = "ocr_unavailable"
            else:
                text = ocr_text.strip()
                extraction = "ocr"
        if text:
            pages.append(Page(text=text, page_no=i + 1, extraction=extraction))
    return pages


def extract_image(path: Path) -> list[Page]:
    from PIL import Image, ImageSequence

    pages: list[Page] = []
    with Image.open(path) as img:
        for i, frame in enumerate(ImageSequence.Iterator(img)):
            text = _ocr_image(frame.convert("RGB"))
            if text is None:
                pages.append(Page(text="", page_no=i + 1, extraction="ocr_unavailable"))
            elif text.strip():
                pages.append(Page(text=text.strip(), page_no=i + 1, extraction="ocr"))
    return [p for p in pages if p.text]


def extract_docx(path: Path) -> list[Page]:
    """DOCX has no real pages: each heading starts a new 'page' (section)."""
    import docx

    document = docx.Document(str(path))
    pages: list[Page] = []
    section_title: str | None = None
    buffer: list[str] = []
    page_no = 1

    def flush():
        nonlocal page_no, buffer
        text = "\n".join(buffer).strip()
        if text:
            pages.append(Page(text=text, page_no=page_no, section=section_title))
            page_no += 1
        buffer = []

    for para in document.paragraphs:
        txt = para.text.strip()
        if not txt:
            continue
        if para.style is not None and para.style.name.lower().startswith("heading"):
            flush()
            section_title = txt
            buffer.append(txt)
        else:
            buffer.append(txt)
    # Tables: append each table as its own block in the current section
    for table in document.tables:
        rows = []
        for row in table.rows:
            rows.append(" | ".join(c.text.strip() for c in row.cells))
        if rows:
            buffer.append("\n".join(rows))
    flush()
    return pages


def extract_html(path: Path) -> list[Page]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return [Page(text=text, page_no=1, section=title, meta={"title": title})] if text else []


def extract_text(path: Path) -> list[Page]:
    """TXT/MD. Markdown headings become `section` metadata on a per-heading page."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() != ".md":
        return [Page(text=raw.strip(), page_no=1)] if raw.strip() else []

    pages: list[Page] = []
    section: str | None = None
    buffer: list[str] = []
    page_no = 1
    for line in raw.splitlines():
        if line.startswith("#"):
            text = "\n".join(buffer).strip()
            if text:
                pages.append(Page(text=text, page_no=page_no, section=section))
                page_no += 1
            buffer = []
            section = line.lstrip("#").strip()
        buffer.append(line)
    text = "\n".join(buffer).strip()
    if text:
        pages.append(Page(text=text, page_no=page_no, section=section))
    return pages


def extract_csv(path: Path) -> list[Page]:
    """Each block of CSV_ROWS_PER_PAGE rows becomes a page, header repeated on each."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    per_page = max(1, settings.CSV_ROWS_PER_PAGE)
    pages: list[Page] = []
    for i in range(0, len(body), per_page):
        block = body[i : i + per_page]
        lines = [", ".join(header)]
        for r in block:
            lines.append("; ".join(f"{h}: {v}" for h, v in zip(header, r)))
        pages.append(Page(text="\n".join(lines), page_no=len(pages) + 1,
                          section=f"rows {i + 1}-{i + len(block)}"))
    return pages or [Page(text=", ".join(header), page_no=1)]


EXTRACTORS: dict[str, Callable[[Path], list[Page]]] = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "html": extract_html,
    "text": extract_text,
    "csv": extract_csv,
    "image": extract_image,
}


def file_type_for(filename: str) -> str | None:
    return SUPPORTED_EXTENSIONS.get(Path(filename).suffix.lower())


def extract(path: Path) -> list[Page]:
    kind = file_type_for(path.name)
    if kind is None or kind not in EXTRACTORS:
        raise UnsupportedFormat(
            f"Unsupported file type '{path.suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    return EXTRACTORS[kind](path)
