from app.models import Source
from app.rag import extract_citations


def _src(n):
    return Source(n=n, chunk_id=f"d:{n}", doc_id="d", source="f.md", file_type="text", page=n,
                  text="t", rrf_score=0.1)


def test_citations_kept_in_first_use_order_and_bogus_removed():
    sources = [_src(1), _src(2), _src(3)]
    text, cited, unresolved = extract_citations("Fact A [2]. Fact B [1][2]. Fact C [7].", sources)
    assert [c.n for c in cited] == [2, 1]
    assert unresolved == [7]
    assert text == "Fact A [2]. Fact B [1][2]. Fact C."


def test_no_markers():
    text, cited, unresolved = extract_citations("I could not find this in the provided documents.", [_src(1)])
    assert cited == [] and unresolved == [] and text.startswith("I could not")
