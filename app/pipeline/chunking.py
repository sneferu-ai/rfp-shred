"""Section-aware chunking with hierarchical context (FR-007).

Strategy: (1) preserve explicit ``[[PAGE N]]`` markers before concatenation;
(2) identify section boundaries via heading/numbering patterns; (3) target
~6,000 characters per chunk with ~500-character overlap; (4) each chunk
carries its section heading, page range, chunk index, and a section-context
summary for cross-reference awareness. Safety bound: 60 chunks per RFP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TARGET_CHARS = 6000
OVERLAP_CHARS = 500
MAX_CHUNKS = 60

CHUNK_CAP_MESSAGE = (
    "Document exceeded maximum chunk count (60). This RFP may be too long or "
    "too dense for the current chunking strategy. Try splitting the document "
    "or contact support."
)

_HEADING_RE = re.compile(
    r"^(?P<head>(?:SECTION|Section|PART|Part)\s+[A-Z0-9][^\n]{0,100}"
    r"|(?:END|End)\s+of\s+(?:SOLICITATION|solicitation|DOCUMENT|document)[^\n]{0,100}"
    r"|(?:[A-Z]\.\d+(?:\.\d+)*(?:\s*\.\s*[a-z])?)\s+[^\n]{3,100})\s*$",
    re.M,
)
_PARENT_RE = re.compile(r"^(?:SECTION|Section|PART|Part)\s+([A-Z0-9]+)")
_REQUIREMENT_MODAL_RE = re.compile(
    r"\b(shall|must|will|is required to|is responsible for|are required to)\b",
    re.I,
)


class ChunkCapExceeded(RuntimeError):
    def __init__(self) -> None:
        super().__init__(CHUNK_CAP_MESSAGE)


@dataclass
class Chunk:
    index: int
    section_heading: str
    parent_section: str
    page_start: int
    page_end: int
    text: str
    context_summary: str = ""
    sub_index: int = 0  # 0 = original; >0 marks halved sub-chunks


@dataclass
class _Segment:
    heading: str
    parent: str
    start: int
    end: int


def _first_sentence(text: str, limit: int = 160) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"\.\s", flat)
    cut = m.end() - 1 if m else len(flat)
    return flat[: min(cut, limit)]


def _page_for_offset(page_spans: list[tuple[int, int, int]], offset: int) -> int:
    for page, start, end in page_spans:
        if start <= offset < end:
            return page
    return page_spans[-1][0] if page_spans else 1


def build_chunks(
    pages: dict[int, str],
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
    max_chunks: int = MAX_CHUNKS,
) -> list[Chunk]:
    ordered = [(p, pages[p]) for p in sorted(pages)]
    if not ordered or not any(text.strip() for _, text in ordered):
        return []
    full = ""
    page_spans: list[tuple[int, int, int]] = []
    for page, text in ordered:
        start = len(full)
        full += f"[[PAGE {page}]]\n{text}\n"
        page_spans.append((page, start, len(full)))
    # Section boundaries
    boundaries: list[tuple[int, str]] = [(0, "Document")]
    for m in _HEADING_RE.finditer(full):
        heading = m.group("head").strip()
        # Numbered requirements such as ``L.1 The offeror shall submit`` are
        # rows, not section headings. Treating them as boundaries leaks the
        # prior requirement text into the ``section`` field of later pages.
        if _REQUIREMENT_MODAL_RE.search(heading):
            continue
        boundaries.append((m.start(), heading))
    # dedupe + sort by offset, drop the synthetic "Document" if a real
    # heading starts at offset 0
    seen: set[int] = set()
    uniq: list[tuple[int, str]] = []
    for off, head in sorted(boundaries):
        if off in seen:
            continue
        seen.add(off)
        uniq.append((off, head))
    if len(uniq) > 1 and uniq[1][0] == 0:
        uniq = uniq[1:]

    segments: list[_Segment] = []
    for i, (off, head) in enumerate(uniq):
        end = uniq[i + 1][0] if i + 1 < len(uniq) else len(full)
        pm = _PARENT_RE.match(head)
        parent = ""
        if not pm:
            for j in range(i - 1, -1, -1):
                if _PARENT_RE.match(uniq[j][1]):
                    parent = _PARENT_RE.match(uniq[j][1]).group(0)  # type: ignore[union-attr]
                    break
        segments.append(_Segment(heading=head, parent=parent, start=off, end=end))

    # Context summaries: one-sentence summary of preceding sections.
    chunks: list[Chunk] = []
    for si, seg in enumerate(segments):
        seg_text = full[seg.start:seg.end]
        preceding = []
        for prev in segments[max(0, si - 3):si]:
            summary = _first_sentence(full[prev.start:prev.end], 120)
            preceding.append(f"{prev.heading}: {summary}")
        context = "Preceding sections -> " + " | ".join(preceding) if preceding else ""
        step = max(1, target_chars - overlap_chars)
        pos = 0
        while pos < len(seg_text):
            piece = seg_text[pos:pos + target_chars]
            abs_start = seg.start + pos
            abs_end = abs_start + len(piece)
            chunks.append(
                Chunk(
                    index=len(chunks),
                    section_heading=seg.heading,
                    parent_section=seg.parent,
                    page_start=_page_for_offset(page_spans, abs_start),
                    page_end=_page_for_offset(page_spans, max(abs_start, abs_end - 1)),
                    text=piece,
                    context_summary=context,
                )
            )
            if abs_end >= seg.end:
                break
            pos += step
    if len(chunks) > max_chunks:
        raise ChunkCapExceeded()
    return chunks


def halve_chunk(chunk: Chunk) -> tuple[Chunk, Chunk]:
    """Split a chunk into two overlapping halves (truncation / >25-item
    handling). Sub-chunks inherit section context; page ranges are split
    proportionally."""
    mid = len(chunk.text) // 2
    overlap = min(OVERLAP_CHARS // 2, mid)
    first_text = chunk.text[: mid + overlap]
    second_text = chunk.text[max(0, mid - overlap):]
    mid_page = chunk.page_start + (chunk.page_end - chunk.page_start) // 2
    first = Chunk(
        index=chunk.index,
        section_heading=chunk.section_heading,
        parent_section=chunk.parent_section,
        page_start=chunk.page_start,
        page_end=max(chunk.page_start, mid_page),
        text=first_text,
        context_summary=chunk.context_summary,
        sub_index=chunk.sub_index + 1,
    )
    second = Chunk(
        index=chunk.index,
        section_heading=chunk.section_heading,
        parent_section=chunk.parent_section,
        page_start=max(chunk.page_start, mid_page),
        page_end=chunk.page_end,
        text=second_text,
        context_summary=chunk.context_summary,
        sub_index=chunk.sub_index + 1,
    )
    return first, second
