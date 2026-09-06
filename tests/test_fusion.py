from app.retrieval.fusion import rrf_merge


def test_rrf_prefers_items_in_both_lists():
    fused = rrf_merge({"dense": ["a", "b", "c"], "bm25": ["c", "d", "a"]}, k=60)
    order = [cid for cid, _, _ in fused]
    assert order[:2] == ["a", "c"]  # a: ranks 1+3, c: ranks 3+1 -> tie broken by id
    ranks = {cid: r for cid, _, r in fused}
    assert ranks["a"] == {"dense": 1, "bm25": 3}
    assert ranks["b"] == {"dense": 2}
    assert ranks["d"] == {"bm25": 2}


def test_rrf_scores_formula():
    fused = rrf_merge({"dense": ["x"]}, k=60)
    assert fused[0][1] == 1 / 61
