"""FR-007 clause_id normalization (AC-049) + FR-008 text normalization."""

from app.pipeline.text_utils import (
    fuzzy_contains,
    levenshtein_distance,
    normalize_clause_id,
    normalize_text,
)


def test_clause_id_variants_collapse_to_one_form():
    variants = ["L.3.2.1", "L 3 2 1", "L.3-2-1", "L.3.2.1."]
    normalized = {normalize_clause_id(v) for v in variants}
    assert normalized == {"l.3.2.1"}


def test_clause_id_normalization_is_idempotent():
    for raw in ["M.1.a", "L 3 2 1", "L.3-2-1.", "ABC", "  X.1  "]:
        once = normalize_clause_id(raw)
        assert normalize_clause_id(once) == once


def test_clause_id_edge_cases():
    assert normalize_clause_id("") == ""
    assert normalize_clause_id("...") == ""
    assert normalize_clause_id("A--B") == "a.b"
    assert normalize_clause_id("L.3.2.1;") == "l.3.2.1"


def test_normalize_text_ligatures_and_hyphen_breaks():
    assert normalize_text("Certi\ufb01cation re\ufb02ects word-\nwrapped text") == "certification reflects wordwrapped text"
    assert normalize_text("  Multiple   spaces\tand\nnewlines ") == "multiple spaces and newlines"
    assert normalize_text("") == ""


def test_levenshtein_distance_basics():
    assert levenshtein_distance("abc", "abc") == 0
    assert levenshtein_distance("abc", "abd") == 1
    assert levenshtein_distance("kitten", "sitting") == 3
    # banded cutoff: distance provably exceeds cutoff
    assert levenshtein_distance("aaaa", "bbbb", cutoff=2) == 3


def test_fuzzy_contains_substring_and_fuzzy():
    page = normalize_text("The offeror shall submit a technical volume not to exceed 50 pages total.")
    ok, _ = fuzzy_contains(normalize_text("shall submit a technical volume"), page)
    assert ok
    # one-character OCR noise inside the excerpt still passes (ratio <= 0.10)
    ok, detail = fuzzy_contains(normalize_text("shall subm1t a technical volume"), page)
    assert ok, detail
    # completely foreign excerpt fails
    ok, _ = fuzzy_contains(normalize_text("entirely unrelated words about bananas"), page)
    assert not ok
    ok, _ = fuzzy_contains("", page)
    assert not ok
    ok, _ = fuzzy_contains("anything", "")
    assert not ok
