"""
Turn a file into a list of `Page`s (text + page metadata).

Two-tier design:
  1. PRIMARY extractor per format — PyMuPDF (PDF), python-docx (DOCX), beautifulsoup4 (HTML),
     stdlib (TXT/MD/CSV). Fast and exact when the file contains real text.
  2. FALLBACK — used only when the primary extractor raises or yields no text:
       PDF   → render every page with PyMuPDF and read it with Tesseract OCR
       DOCX  → OCR the images embedded in the document (word/media/*)
       HTML  → stdlib tag-stripping (no pixels to OCR)
       image → Tesseract is the primary and only path (there is no text layer)
     Inside the PDF primary path, individual pages with fewer than OCR_MIN_CHARS_PER_PAGE
     characters (scanned pages in an otherwise digital PDF) are OCR'd page by page.

Dispatch is driven by `app.config.SUPPORTED_EXTENSIONS`; each extractor name there must exist
in `EXTRACTORS`. To support a new format add a primary function (and optionally a fallback),
register both, and add the extension.

If no text can be obtained at all, `extract()` raises `ExtractionError` with a precise reason
(e.g. "Tesseract OCR is not installed") which becomes the job's FAILED reason.
"""
from __future__ import annotations

import csv
import io
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from app.config import SUPPORTED_EXTENSIONS, settings

log = logging.getLogger(__name__)


@dataclass
class Page:
    text: str
    page_no: int
    section: str | None = None
    extraction: str = "text"  # text | ocr
    meta: dict = field(default_factory=dict)


class UnsupportedFormat(ValueError):
    pass


class ExtractionError(ValueError):
    """Primary and fallback extraction both failed; the message is user-facing."""


# --------------------------------------------------------------------------- OCR

def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _require_ocr(what: str) -> None:
    if not settings.OCR_ENABLED:
        raise ExtractionError(f"{what} contains no text layer and OCR is disabled (OCR_ENABLED=false).")
    if not ocr_available():
        raise ExtractionError(
            f"{what} contains no text layer and Tesseract OCR is not installed on this host, so it "
            "could not be read. OCR works in the Docker image; locally install tesseract "
            "(brew install tesseract / apt install tesseract-ocr) and upload again.")


def _ocr_image(image) -> str:
    import pytesseract

    return (pytesseract.image_to_string(image, lang=settings.OCR_LANG) or "").strip()


def _pixmap_to_image(pix):
    from PIL import Image

    mode = "RGBA" if pix.alpha else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples).convert("RGB")


def _ocr_pdf_page(page) -> str:
    return _ocr_image(_pixmap_to_image(page.get_pixmap(dpi=settings.OCR_DPI)))


# ------------------------------------------------------------------ PDF (PyMuPDF)

def extract_pdf(path: Path) -> list[Page]:
    """PyMuPDF text per page; pages without a usable text layer are OCR'd individually."""
    import fitz  # PyMuPDF

    pages: list[Page] = []
    with fitz.open(str(path)) as doc:
        can_ocr = settings.OCR_ENABLED and ocr_available()
        for i, page in enumerate(doc):
            text = (page.get_text("text") or "").strip()
            extraction = "text"
            if len(text) < settings.OCR_MIN_CHARS_PER_PAGE and can_ocr:
                ocr_text = _ocr_pdf_page(page)
                if ocr_text:
                    text, extraction = ocr_text, "ocr"
            if text:
                pages.append(Page(text=text, page_no=i + 1, extraction=extraction))
    return pages


def fallback_pdf(path: Path) -> list[Page]:
    """Primary produced nothing: the PDF is scanned/image-only. OCR every page."""
    import fitz

    doc = fitz.open(str(path))  # raises on a corrupt file -> "Could not read this pdf"
    _require_ocr("This PDF")
    pages: list[Page] = []
    with doc:
        for i, page in enumerate(doc):
            text = _ocr_pdf_page(page)
            if text:
                pages.append(Page(text=text, page_no=i + 1, extraction="ocr"))
    return pages


# ------------------------------------------------------------------------ images

def extract_image(path: Path) -> list[Page]:
    from PIL import Image, ImageSequence

    _require_ocr("This image")
    pages: list[Page] = []
    with Image.open(path) as img:
        for i, frame in enumerate(ImageSequence.Iterator(img)):
            text = _ocr_image(frame.convert("RGB"))
            if text:
                pages.append(Page(text=text, page_no=i + 1, extraction="ocr"))
    return pages


# ------------------------------------------------------------------------- DOCX

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
    for table in document.tables:
        rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
        if rows:
            buffer.append("\n".join(rows))
    flush()
    return pages


def fallback_docx(path: Path) -> list[Page]:
    """No text in the document body: OCR the embedded images (scanned pages pasted into Word)."""
    from PIL import Image

    _require_ocr("This DOCX")
    pages: list[Page] = []
    with zipfile.ZipFile(path) as zf:
        media = sorted(n for n in zf.namelist() if n.startswith("word/media/"))
        for i, name in enumerate(media):
            try:
                with zf.open(name) as fh:
                    text = _ocr_image(Image.open(io.BytesIO(fh.read())).convert("RGB"))
            except Exception as exc:  # unsupported image type etc.
                log.debug("skip %s: %s", name, exc)
                continue
            if text:
                pages.append(Page(text=text, page_no=i + 1, section=Path(name).name, extraction="ocr"))
    return pages


# ------------------------------------------------------------------------- HTML

def extract_html(path: Path) -> list[Page]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return [Page(text=text, page_no=1, section=title, meta={"title": title})] if text else []


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def fallback_html(path: Path) -> list[Page]:
    """beautifulsoup/lxml choked: strip tags with the stdlib parser."""
    parser = _Stripper()
    parser.feed(path.read_bytes().decode("utf-8", errors="replace"))
    text = "\n".join(parser.parts)
    return [Page(text=text, page_no=1)] if text else []


# ------------------------------------------------------------------- TXT / MD / CSV

def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_text(path: Path) -> list[Page]:
    """TXT/MD. Markdown headings become `section` metadata on a per-heading page."""
    raw = _read_text(path)
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
    reader = csv.reader(io.StringIO(_read_text(path)))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    per_page = max(1, settings.CSV_ROWS_PER_PAGE)
    pages: list[Page] = []
    for i in range(0, len(body), per_page):
        block = body[i:i + per_page]
        lines = [", ".join(header)] + ["; ".join(f"{h}: {v}" for h, v in zip(header, r)) for r in block]
        pages.append(Page(text="\n".join(lines), page_no=len(pages) + 1,
                          section=f"rows {i + 1}-{i + len(block)}"))
    return pages or [Page(text=", ".join(header), page_no=1)]


# ------------------------------------------------------------------------ registry

EXTRACTORS: dict[str, Callable[[Path], list[Page]]] = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "html": extract_html,
    "text": extract_text,
    "csv": extract_csv,
    "image": extract_image,
}

FALLBACKS: dict[str, Callable[[Path], list[Page]]] = {
    "pdf": fallback_pdf,
    "docx": fallback_docx,
    "html": fallback_html,
}


def file_type_for(filename: str) -> str | None:
    return SUPPORTED_EXTENSIONS.get(Path(filename).suffix.lower())


def extract(path: Path) -> list[Page]:
    """Primary extractor first; on exception or empty result, the format's fallback."""
    kind = file_type_for(path.name)
    if kind is None or kind not in EXTRACTORS:
        raise UnsupportedFormat(
            f"Unsupported file type '{path.suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}")

    primary_error: Exception | None = None
    pages: list[Page] = []
    try:
        pages = EXTRACTORS[kind](path)
    except ExtractionError:
        raise
    except Exception as exc:
        primary_error = exc
        log.warning("primary extractor for %s failed on %s: %s", kind, path.name, exc)
    if pages:
        return pages

    fallback = FALLBACKS.get(kind)
    if fallback is None:
        if primary_error is not None:
            raise ExtractionError(f"Could not read this {kind} file: {primary_error}") from primary_error
        raise ExtractionError("The file contains no extractable text.")

    log.info("using fallback extractor for %s (%s)", path.name, kind)
    try:
        pages = fallback(path)
    except ExtractionError:
        raise
    except Exception as exc:
        cause = primary_error or exc
        raise ExtractionError(f"Could not read this {kind} file: {cause}") from exc
    if not pages:
        raise ExtractionError("The file contains no extractable text, even after the fallback extractor.")
    return pages
