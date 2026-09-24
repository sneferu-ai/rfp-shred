"""FR-008 citation audit + FR-009 filter split."""

from app.pipeline.audit import audit_items, locate_source_pages
from app.pipeline.filter import FILTER_REASON, split_requirements
from app.pipeline.mine import MinedItem


def _item(page: int, excerpt: str, text: str | None = None, is_requirement: bool = True, order: int = 0) -> MinedItem:
    return MinedItem(
        clause_id="L.1", section="Section L", page=page,
        text=text or excerpt, excerpt=excerpt, is_requirement=is_requirement,
        chunk_index=0, doc_order=order,
    )


PAGES = {
    1: "Section L — Instructions. The offeror shall submit a technical volume not to exceed 50 pages.",
    2: "M.1 The Government will evaluate proposals for technical merit and price.",
}


def test_verbatim_excerpt_passes():
    outcome = audit_items([_item(1, "shall submit a technical volume")], PAGES)
    assert len(outcome.passed) == 1
    assert not outcome.failures


def test_fuzzy_ocr_noise_passes():
    # excerpt with a couple of OCR-style character substitutions
    outcome = audit_items([_item(1, "shall subm1t a techn1cal volume")], PAGES)
    assert len(outcome.passed) == 1


def test_unverifiable_excerpt_drops_with_reason():
    outcome = audit_items([_item(1, "bananas are required for lunch")], PAGES)
    assert not outcome.passed
    assert len(outcome.failures) == 1
    assert "not verified" in outcome.failures[0].reason


def test_verified_excerpt_cannot_hide_fabricated_requirement_body():
    outcome = audit_items(
        [_item(1, "shall submit a technical volume", text="Send the contracting officer a yacht.")],
        PAGES,
    )
    assert not outcome.passed
    assert len(outcome.failures) == 1
    assert "requirement text not verified" in outcome.failures[0].reason


def test_cited_page_out_of_range_drops():
    outcome = audit_items([_item(99, "anything")], PAGES)
    assert not outcome.passed
    assert "outside document range" in outcome.failures[0].reason


def test_ligature_and_hyphen_normalization_in_audit():
    pages = {3: "The oﬀeror shall deliver word-\nwrapped goods."}
    outcome = audit_items([_item(3, "offeror shall deliver wordwrapped goods")], pages)
    assert len(outcome.passed) == 1


def test_unique_verified_source_text_corrects_model_page():
    item = _item(2, "shall submit a technical volume")
    locate_source_pages([item], PAGES)
    assert item.page == 1
    assert audit_items([item], PAGES).passed == [item]


def test_filter_split_routes_non_binding():
    items = [
        _item(1, "shall submit", is_requirement=True, order=0),
        _item(1, "table of contents", is_requirement=False, order=1),
    ]
    outcome = split_requirements(items)
    assert len(outcome.requirements) == 1
    assert len(outcome.filtered) == 1
    assert outcome.filtered[0][1] == FILTER_REASON
