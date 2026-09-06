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

Dispatch is driven by `app.config.SUPPORTED_EXTENSIONS`, but the *content* wins over the
extension: `sniff_type()` looks at the first bytes (PK zip → docx, `{\\rtf` → rtf, `%PDF` → pdf,
OLE header → legacy .doc, `<html` → html, image magic → image). A file named `.docx` that is
really RTF is therefore read by the RTF extractor instead of failing. Each extractor name must
exist in `EXTRACTORS`. To support a new format add a primary function (and optionally a fallback),
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


# ------------------------------------------------------------------- sniffing

_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"GIF8", b"BM")


def sniff_type(path: Path) -> str | None:
    """Detect the real format from the first bytes. Returns an extractor name, "doc" for legacy
    binary Word files (not supported), or None when the bytes are not conclusive."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2048)
    except OSError:
        return None
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"  # OLE2 compound file: legacy .doc / .xls / .ppt
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            if any(n.startswith("word/") for n in names):
                return "docx"
        except zipfile.BadZipFile:
            return None
        return None
    if head.startswith(_IMAGE_MAGIC):
        return "image"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html")):
        return "html"
    return None


# --------------------------------------------------------------------------- OCR

_TESSERACT_CANDIDATES = (
    "/opt/homebrew/bin/tesseract",      # Homebrew on Apple silicon (often not on a service's PATH)
    "/usr/local/bin/tesseract",         # Homebrew on Intel macs / manual installs
    "/opt/local/bin/tesseract",         # MacPorts
    "/usr/bin/tesseract",               # apt / Docker image
    "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
)


def tesseract_cmd() -> str | None:
    """Resolve the tesseract binary: TESSERACT_CMD, then PATH, then common install locations.
    Also points pytesseract at it, so a binary outside PATH works."""
    found: str | None = None
    if settings.TESSERACT_CMD:
        found = settings.TESSERACT_CMD if Path(settings.TESSERACT_CMD).is_file() else None
    else:
        found = shutil.which("tesseract") or next((c for c in _TESSERACT_CANDIDATES if Path(c).is_file()), None)
    if found:
        try:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = found
        except ImportError:  # pragma: no cover
            return None
    return found


def ocr_available() -> bool:
    return tesseract_cmd() is not None


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
                if len(ocr_text) > len(text):  # keep the real text layer when OCR finds nothing better
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

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_blocks(container):
    """Yield ("heading"|"para"|"row", text) for a DOCX body in document order, descending into
    content controls (w:sdt / w:sdtContent) and tables — python-docx's `document.paragraphs`
    ignores both, which makes contract templates built from content controls come out empty."""
    for child in container:
        tag = child.tag
        if tag == f"{_W}p":
            text = "".join(t.text or "" for t in child.iter(f"{_W}t", f"{_W}tab", f"{_W}br"))
            text = "".join(("\t" if t.tag == f"{_W}tab" else "\n" if t.tag == f"{_W}br" else (t.text or ""))
                           for t in child.iter(f"{_W}t", f"{_W}tab", f"{_W}br")).strip()
            if not text:
                continue
            style = child.find(f"{_W}pPr/{_W}pStyle")
            val = (style.get(f"{_W}val") if style is not None else "") or ""
            yield ("heading" if val.lower().startswith(("heading", "title")) else "para"), text
        elif tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = []
                for cell in row.findall(f"{_W}tc"):
                    cells.append(" ".join(t for _, t in _docx_blocks(cell)))
                if any(cells):
                    yield "row", " | ".join(cells)
        elif tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}smartTag", f"{_W}ins", f"{_W}hyperlink",
                     f"{_W}customXml", f"{_W}fldSimple"):
            yield from _docx_blocks(child)
        elif tag == f"{_W}sectPr":
            continue
        elif len(child):
            yield from _docx_blocks(child)


def extract_docx(path: Path) -> list[Page]:
    """DOCX has no real pages: each heading starts a new 'page' (section). Walks the body XML in
    order (including content controls and tables) via python-docx's element tree."""
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

    for kind, text in _docx_blocks(document.element.body):
        if kind == "heading":
            flush()
            section_title = text
        buffer.append(text)
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


# -------------------------------------------------------------------------- RTF

def extract_rtf(path: Path) -> list[Page]:
    """RTF via striprtf. \\page control words become page breaks; the first line of each page
    is used as its section label when it looks like a heading."""
    import re

    from striprtf.striprtf import rtf_to_text

    raw = path.read_bytes().decode("latin-1", errors="replace")
    # striprtf drops \page silently; turn it into a text marker first so we can split on it.
    marker = "RAGPAGEBREAK7F3A"
    raw = re.sub(r"\\page\b", lambda m: "\\par " + marker + "\\par ", raw)  # lambda: no re-escaping of \p
    text = rtf_to_text(raw, errors="ignore")
    pages: list[Page] = []
    for i, block in enumerate(text.split(marker)):
        block = "\n".join(ln.rstrip() for ln in block.splitlines()).strip()
        if block:
            first = block.splitlines()[0].strip()
            section = first if 0 < len(first) <= 80 and len(block.splitlines()) > 1 else None
            pages.append(Page(text=block, page_no=i + 1, section=section))
    return pages


def fallback_rtf(path: Path) -> list[Page]:
    """striprtf choked: crude control-word stripping with the stdlib."""
    import re

    raw = path.read_bytes().decode("latin-1", errors="replace")
    raw = re.sub(r"\\'[0-9a-f]{2}", " ", raw)                 # hex escapes
    raw = re.sub(r"{\\\*[^{}]*}", " ", raw)                        # destinations
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", raw)                # control words
    raw = re.sub(r"[{}]", " ", raw)
    text = re.sub(r"[ \t]+", " ", raw)
    text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
    return [Page(text=text, page_no=1)] if len(text) > 20 else []


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
    "rtf": extract_rtf,
    "image": extract_image,
}

FALLBACKS: dict[str, Callable[[Path], list[Page]]] = {
    "pdf": fallback_pdf,
    "docx": fallback_docx,
    "html": fallback_html,
    "rtf": fallback_rtf,
}

# Formats we can recognise from the bytes but cannot read, with the advice to show the user.
_UNREADABLE = {
    "doc": "This is a legacy binary Word file (.doc / OLE2), not a DOCX package. Open it in Word or "
           "LibreOffice and save as .docx (or export to PDF), then upload again.",
}


def file_type_for(filename: str) -> str | None:
    return SUPPORTED_EXTENSIONS.get(Path(filename).suffix.lower())


def resolve_type(path: Path) -> tuple[str, str | None]:
    """(extractor to use, note) — the sniffed content type overrides the extension when they differ."""
    by_ext = file_type_for(path.name)
    sniffed = sniff_type(path) if settings.SNIFF_CONTENT else None
    if by_ext is None or by_ext not in EXTRACTORS:
        if sniffed in EXTRACTORS:  # unknown extension but recognisable content (e.g. a PDF named .bin)
            note = f"extension '{path.suffix or '(none)'}' is not registered but content is {sniffed.upper()}; read as {sniffed}"
            log.info("[extract] %s: %s", path.name, note)
            return sniffed, note
        raise UnsupportedFormat(
            f"Unsupported file type '{path.suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}")
    if not settings.SNIFF_CONTENT:
        return by_ext, None
    if sniffed in _UNREADABLE:
        raise ExtractionError(f"File '{_display_name(path)}' has extension '{path.suffix}' but its content is a legacy "
                              f"binary Word document. {_UNREADABLE[sniffed]}")
    if sniffed and sniffed != by_ext and sniffed in EXTRACTORS:
        note = f"extension '{path.suffix}' but content is {sniffed.upper()}; read as {sniffed}"
        log.info("[extract] %s: %s", path.name, note)
        return sniffed, note
    return by_ext, None


def _brief(exc: BaseException, path: Path, limit: int = 160) -> str:
    """`TypeName: message` with absolute paths replaced by the file name and the length capped."""
    msg = str(exc).replace(str(path.resolve()), path.name).replace(str(path), path.name).strip()
    msg = " ".join(msg.split())
    if len(msg) > limit:
        msg = msg[:limit].rstrip() + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _display_name(path: Path) -> str:
    """Uploaded files are stored as <doc_id>_<original name>; show the original name."""
    stem, sep, rest = path.name.partition("_")
    return rest if sep and len(stem) == 12 and all(c in "0123456789abcdef" for c in stem) else path.name


def extract(path: Path) -> list[Page]:
    """Primary extractor first (chosen by content, then extension); on exception or empty result,
    the format's fallback. Raises ExtractionError with a user-facing reason when nothing works."""
    kind, note = resolve_type(path)
    shown = _display_name(path)

    primary_error: Exception | None = None
    pages: list[Page] = []
    try:
        log.info("[extract] primary=%s file=%s", kind, path.name)
        pages = EXTRACTORS[kind](path)
        log.info("[extract] primary=%s file=%s pages=%d", kind, path.name, len(pages))
    except ExtractionError:
        raise
    except Exception as exc:
        primary_error = exc
        log.warning("[extract] primary=%s file=%s FAILED: %s: %s", kind, path.name, type(exc).__name__, exc)
    if pages:
        if note:
            for pg in pages:
                pg.meta["format_note"] = note
        return pages

    fallback = FALLBACKS.get(kind)
    why = (f"the {kind} reader failed ({_brief(primary_error, path)})" if primary_error
           else f"the {kind} reader found no text")
    if fallback is None:
        raise ExtractionError(f"Could not read '{shown}': {why}.")

    log.info("[extract] fallback=%s file=%s because %s", kind, path.name, why)
    try:
        pages = fallback(path)
    except ExtractionError as exc:
        # keep the OCR / configuration message but say what went wrong first
        raise ExtractionError(f"{why[0].upper() + why[1:]}. Fallback: {exc}") from exc
    except Exception as exc:
        same = primary_error is not None and str(exc) == str(primary_error)
        tail = "the fallback could not open the file either" if same else f"fallback also failed ({_brief(exc, path)})"
        raise ExtractionError(f"Could not read '{shown}': {why}; {tail}.") from exc
    if not pages:
        raise ExtractionError(f"Could not read '{shown}': {why}, and the fallback extractor found no text either.")
    log.info("[extract] fallback=%s file=%s pages=%d", kind, path.name, len(pages))
    return pages
