"""FR-007 chunking: boundaries, size/overlap, 60-chunk cap, context, halving."""

import pytest

from app.pipeline.chunking import (
    CHUNK_CAP_MESSAGE,
    MAX_CHUNKS,
    ChunkCapExceeded,
    build_chunks,
    halve_chunk,
)


def _pages(count: int, chars_per_page: int = 1000, prefix: str = "Text") -> dict[int, str]:
    return {p: f"{prefix} page {p} " + ("x" * chars_per_page) for p in range(1, count + 1)}


def test_section_boundaries_detected():
    pages = {
        1: "SOLICITATION 123\nSection B\nSupplies or services.\n",
        2: "Section L — Instructions to Offerors\nL.1 The offeror shall submit.\n",
        3: "Section M — Evaluation\nM.1 The Government will evaluate.\n",
    }
    chunks = build_chunks(pages, target_chars=6000, overlap_chars=500)
    headings = [c.section_heading for c in chunks]
    assert any("Section L" in h for h in headings)
    assert any("Section M" in h for h in headings)


def test_numbered_requirement_is_not_a_section_heading():
    pages = {
        1: "Section L — Instructions\nL.1 The offeror shall submit a volume.",
        2: "End of solicitation ABC-1.\nAll proposals must remain valid for 90 days.",
    }
    chunks = build_chunks(pages, target_chars=6000, overlap_chars=500)
    headings = [chunk.section_heading for chunk in chunks]
    assert "L.1 The offeror shall submit a volume." not in headings
    assert "End of solicitation ABC-1." in headings


def test_chunk_sizes_and_overlap():
    pages = _pages(20, chars_per_page=2000)  # ~40k chars
    chunks = build_chunks(pages, target_chars=6000, overlap_chars=500)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert len(chunk.text) <= 6000
    # overlap: consecutive chunks share text
    assert chunks[0].text[-100:] == chunks[1].text[:100] or chunks[0].text[-400:-300] in chunks[1].text


def test_page_ranges_tracked():
    pages = _pages(12, chars_per_page=2000)
    chunks = build_chunks(pages, target_chars=6000, overlap_chars=500)
    for chunk in chunks:
        assert 1 <= chunk.page_start <= chunk.page_end <= 12


def test_chunks_preserve_explicit_page_boundaries():
    chunks = build_chunks(
        {
            1: "Section L\nL.1 The offeror shall submit volume one.",
            2: "L.2 The offeror must submit volume two.",
        },
        target_chars=6000,
        overlap_chars=500,
    )
    joined = "\n".join(chunk.text for chunk in chunks)
    assert "[[PAGE 1]]" in joined
    assert "[[PAGE 2]]" in joined


def test_chunk_cap_raises_with_exact_message():
    pages = _pages(400, chars_per_page=1000)  # 400k chars -> ~80 chunks at 5k step
    with pytest.raises(ChunkCapExceeded) as excinfo:
        build_chunks(pages, target_chars=6000, overlap_chars=500)
    assert str(excinfo.value) == CHUNK_CAP_MESSAGE
    assert "60" in CHUNK_CAP_MESSAGE


def test_context_summary_carries_preceding_sections():
    pages = {
        1: "Section B\nThis section describes supplies and services for the effort.",
        2: "Section L\n" + ("L.1 The offeror shall do many things. " * 400),
    }
    chunks = build_chunks(pages, target_chars=2000, overlap_chars=200)
    later = [c for c in chunks if c.section_heading.startswith("Section L")]
    assert later
    assert any("Section B" in (c.context_summary or "") for c in later[1:])


def test_halve_chunk_preserves_context_and_pages():
    pages = _pages(8, chars_per_page=2000)
    chunk = build_chunks(pages, target_chars=6000, overlap_chars=500)[0]
    first, second = halve_chunk(chunk)
    assert first.section_heading == chunk.section_heading
    assert second.context_summary == chunk.context_summary
    assert first.page_start == chunk.page_start
    assert second.page_end == chunk.page_end
    assert first.sub_index == chunk.sub_index + 1
    assert len(first.text) + len(second.text) >= len(chunk.text)


def test_empty_document_produces_no_chunks():
    assert build_chunks({}) == []
    assert build_chunks({1: ""}) == []
