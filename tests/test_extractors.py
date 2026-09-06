from pathlib import Path

import pytest

from app.ingest import extractors
from app.ingest.extractors import UnsupportedFormat, extract, file_type_for


def test_file_type_mapping():
    assert file_type_for("a.PDF") == "pdf"
    assert file_type_for("a.md") == "text"
    assert file_type_for("a.xyz") is None


def test_markdown_sections(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\nintro text\n\n## Part A\nbody a\n\n## Part B\nbody b\n")
    pages = extract(p)
    assert [pg.section for pg in pages] == ["Title", "Part A", "Part B"]
    assert [pg.page_no for pg in pages] == [1, 2, 3]


def test_csv_pages(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extractors.settings, "CSV_ROWS_PER_PAGE", 2)
    p = tmp_path / "t.csv"
    p.write_text("name,age\nann,30\nbob,40\ncid,50\n")
    pages = extract(p)
    assert len(pages) == 2
    assert "name: ann" in pages[0].text and "age: 50" in pages[1].text


def test_html_strips_scripts(tmp_path: Path):
    p = tmp_path / "x.html"
    p.write_text("<html><head><title>T</title><script>var x=1;</script></head>"
                 "<body><p>Hello</p><p>World</p></body></html>")
    pages = extract(p)
    assert pages[0].section == "T"
    assert "Hello" in pages[0].text and "var x" not in pages[0].text


def test_docx(tmp_path: Path):
    import docx

    d = docx.Document()
    d.add_heading("Chapter 1", level=1)
    d.add_paragraph("First body.")
    d.add_heading("Chapter 2", level=1)
    d.add_paragraph("Second body.")
    p = tmp_path / "d.docx"
    d.save(str(p))
    pages = extract(p)
    assert [pg.section for pg in pages] == ["Chapter 1", "Chapter 2"]


def test_unsupported(tmp_path: Path):
    p = tmp_path / "a.xyz"
    p.write_text("x")
    with pytest.raises(UnsupportedFormat):
        extract(p)


def _blank_pdf(path: Path) -> Path:
    import fitz

    doc = fitz.open()
    doc.new_page(width=200, height=200)
    doc.save(str(path))
    doc.close()
    return path


def _text_pdf(path: Path, text: str) -> Path:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_primary_pymupdf(tmp_path: Path):
    pages = extract(_text_pdf(tmp_path / "t.pdf", "Hello PyMuPDF world"))
    assert len(pages) == 1 and "PyMuPDF" in pages[0].text and pages[0].extraction == "text"


def test_scanned_pdf_without_tesseract_gives_clear_reason(tmp_path: Path, monkeypatch):
    """Image-only PDF + no tesseract -> ExtractionError naming Tesseract, not a crash."""
    monkeypatch.setattr(extractors, "ocr_available", lambda: False)
    with pytest.raises(extractors.ExtractionError, match="Tesseract"):
        extract(_blank_pdf(tmp_path / "blank.pdf"))


def test_scanned_pdf_uses_ocr_fallback(tmp_path: Path, monkeypatch):
    """When OCR is available, the fallback OCRs every page and tags extraction=ocr."""
    monkeypatch.setattr(extractors, "ocr_available", lambda: True)
    monkeypatch.setattr(extractors, "_ocr_image", lambda img: "OCR TEXT FROM PIXELS")
    pages = extract(_blank_pdf(tmp_path / "scan.pdf"))
    assert [(p.text, p.extraction) for p in pages] == [("OCR TEXT FROM PIXELS", "ocr")]


def test_corrupt_pdf_reports_reason(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extractors, "ocr_available", lambda: True)
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf")
    with pytest.raises(extractors.ExtractionError, match="Could not read this pdf"):
        extract(p)


def test_html_fallback_when_bs4_fails(tmp_path: Path, monkeypatch):
    p = tmp_path / "x.html"
    p.write_text("<p>Plain <b>fallback</b> text</p><script>x()</script>")

    def boom(path):
        raise RuntimeError("lxml exploded")

    monkeypatch.setitem(extractors.EXTRACTORS, "html", boom)
    pages = extract(p)
    assert "fallback" in pages[0].text and "x()" not in pages[0].text


def test_image_without_tesseract(tmp_path: Path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(extractors, "ocr_available", lambda: False)
    p = tmp_path / "i.png"
    Image.new("RGB", (50, 50), "white").save(p)
    with pytest.raises(extractors.ExtractionError, match="Tesseract"):
        extract(p)
