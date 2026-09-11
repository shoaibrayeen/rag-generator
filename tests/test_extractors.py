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
    pages = extract(_text_pdf(tmp_path / "t.pdf", "Hello PyMuPDF world, this page has a real text layer."))
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
    with pytest.raises(extractors.ExtractionError, match="Could not read 'bad.pdf': the pdf reader failed"):
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


RTF = (rb"{\rtf1\ansi\deff0{\fonttbl{\f0 Times New Roman;}}\pard Master Services Agreement dated 17 December 2025.\par "
       rb"The Supplier shall deliver the robots within 12 weeks.\par\page Schedule A: pricing is EUR 64,000 per Titan unit.\par}")


def test_sniff_type_detects_content(tmp_path: Path):
    (tmp_path / "a.docx").write_bytes(RTF)
    assert extractors.sniff_type(tmp_path / "a.docx") == "rtf"
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4 ...")
    assert extractors.sniff_type(tmp_path / "b.pdf") == "pdf"
    (tmp_path / "c.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    assert extractors.sniff_type(tmp_path / "c.docx") == "doc"
    (tmp_path / "d.txt").write_bytes(b"just text")
    assert extractors.sniff_type(tmp_path / "d.txt") is None


def test_rtf_mislabelled_as_docx_is_read_as_rtf(tmp_path: Path):
    p = tmp_path / "contract.docx"
    p.write_bytes(RTF)
    pages = extract(p)
    assert len(pages) == 2 and "Master Services Agreement" in pages[0].text and "Schedule A" in pages[1].text
    assert pages[0].meta["format_note"].startswith("extension '.docx' but content is RTF")


def test_rtf_extension(tmp_path: Path):
    p = tmp_path / "memo.rtf"
    p.write_bytes(RTF)
    assert "12 weeks" in extract(p)[0].text


def test_legacy_doc_gives_precise_reason(tmp_path: Path):
    p = tmp_path / "old.docx"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    with pytest.raises(extractors.ExtractionError, match="legacy binary Word"):
        extract(p)


def test_broken_docx_reason_names_the_reader_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extractors, "ocr_available", lambda: False)
    p = tmp_path / "broken.docx"
    p.write_bytes(b"this is not a zip and not rtf")
    with pytest.raises(extractors.ExtractionError) as ei:
        extract(p)
    msg = str(ei.value)
    assert "docx reader failed" in msg and "Fallback:" in msg


@pytest.mark.skipif(extractors.tesseract_cmd() is None, reason="tesseract binary not installed on this host")
def test_real_ocr_on_rendered_image_and_scanned_pdf(tmp_path: Path):
    """Runs only where Tesseract exists: text drawn as pixels must come back through OCR."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 220), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("Helvetica.ttc", 40)
    except OSError:
        f = ImageFont.load_default(size=40)
    d.text((30, 40), "Maintenance window 9 to 11 April 2026", fill="black", font=f)
    d.text((30, 120), "Contact ops at northwind example", fill="black", font=f)
    png = tmp_path / "board.png"
    img.save(png)
    pages = extract(png)
    assert pages and pages[0].extraction == "ocr"
    assert "April 2026" in pages[0].text.replace("\n", " ")

    scanned = tmp_path / "scan.pdf"
    img.save(scanned, "PDF", resolution=150)
    pages = extract(scanned)
    assert pages and pages[0].extraction == "ocr" and "Maintenance" in pages[0].text


def _sdt_docx(path: Path) -> Path:
    """A minimal DOCX whose paragraphs are all wrapped in w:sdt content controls (python-docx sees 0 paragraphs)."""
    import zipfile

    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        '<w:sdt><w:sdtContent><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Terms of Sale</w:t></w:r></w:p></w:sdtContent></w:sdt>'
        '<w:sdt><w:sdtContent><w:p><w:r><w:t>Payment is due within </w:t></w:r><w:r><w:t>30 days</w:t></w:r><w:r><w:t> of invoice.</w:t></w:r></w:p></w:sdtContent></w:sdt>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Price</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>Widget</w:t></w:r></w:p></w:tc><w:tc><w:sdt><w:sdtContent><w:p><w:r><w:t>EUR 10</w:t></w:r></w:p></w:sdtContent></w:sdt></w:tc></w:tr></w:tbl>'
        '<w:sdt><w:sdtContent><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Delivery</w:t></w:r></w:p></w:sdtContent></w:sdt>'
        '<w:p><w:r><w:t>Goods ship within 12 weeks.</w:t></w:r></w:p>'
    )
    doc_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{body}<w:sectPr/></w:body></w:document>'
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    return path


def test_docx_content_controls_and_tables_are_read(tmp_path: Path):
    import docx

    p = _sdt_docx(tmp_path / "template.docx")
    # python-docx alone sees only the single paragraph that is NOT wrapped in a content control
    assert sum(1 for para in docx.Document(str(p)).paragraphs if para.text.strip()) == 1
    pages = extract(p)
    assert [pg.section for pg in pages] == ["Terms of Sale", "Delivery"]
    assert "Payment is due within 30 days of invoice." in pages[0].text
    assert "Widget | EUR 10" in pages[0].text          # table row, including a cell wrapped in a content control
    assert "12 weeks" in pages[1].text
