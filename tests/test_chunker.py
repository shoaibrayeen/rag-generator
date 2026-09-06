from app.ingest.chunker import chunk_pages, split_text
from app.ingest.extractors import Page


def test_split_text_overlaps_and_covers_everything():
    text = " ".join(f"word{i}" for i in range(600))
    spans = split_text(text, size=200, overlap=50)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert s2 < e1, "consecutive windows must overlap"
        assert e1 - s2 <= 50 + 10  # overlap roughly honoured (boundary snapping allowed)
        assert e2 - s2 <= 200


def test_chunks_carry_metadata():
    pages = [Page(text="alpha. " * 300, page_no=3, section="Intro", extraction="ocr")]
    chunks = chunk_pages(pages, doc_id="d1", source="f.pdf", file_type="pdf", collection="c")
    assert len(chunks) > 1
    m = chunks[0].metadata
    assert m["page"] == 3 and m["section"] == "Intro" and m["extraction"] == "ocr"
    assert m["source"] == "f.pdf" and m["doc_id"] == "d1"
    assert chunks[0].chunk_id == "d1:0" and chunks[1].chunk_id == "d1:1"
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_short_text_single_chunk():
    chunks = chunk_pages([Page(text="tiny", page_no=1)], doc_id="d", source="s", file_type="text",
                         collection="c")
    assert len(chunks) == 1 and chunks[0].text == "tiny"
