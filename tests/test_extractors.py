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


def test_pdf_without_tesseract_keeps_going(tmp_path: Path, monkeypatch):
    """A text-less PDF page must not fail ingestion when tesseract is absent."""
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    p = tmp_path / "blank.pdf"
    with open(p, "wb") as fh:
        w.write(fh)
    monkeypatch.setattr(extractors, "ocr_available", lambda: False)
    pages = extract(p)
    assert pages == []  # blank page, no text, no crash
