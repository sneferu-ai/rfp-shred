"""Text normalization utilities (FR-007 clause ids, FR-008 citation audit)."""

from __future__ import annotations

import re

_LIGATURES = {
    "\ufb01": "fi",  # ﬁ
    "\ufb02": "fl",  # ﬂ
    "\ufb00": "ff",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}

_WS_RUN = re.compile(r"\s+")
_HYPHEN_LINEBREAK = re.compile(r"-\n")
_MULTI_DOT = re.compile(r"\.{2,}")
_WS_OR_HYPHEN = re.compile(r"[\s\-]+")


def normalize_text(text: str) -> str:
    """FR-008 normalization: join hyphenated line breaks, replace OCR
    ligatures, collapse repeated whitespace + trim, lowercase."""
    if not text:
        return ""
    out = _HYPHEN_LINEBREAK.sub("", text)
    for lig, repl in _LIGATURES.items():
        out = out.replace(lig, repl)
    out = _WS_RUN.sub(" ", out).strip().lower()
    return out


def normalize_clause_id(clause_id: str) -> str:
    """FR-007 clause_id normalization (OBL-5).

    Lowercase; strip trailing punctuation; convert whitespace runs and
    hyphens to ``.``; collapse consecutive dots; strip edge dots.
    Idempotent: normalize(normalize(x)) == normalize(x).
    ``L.3.2.1``, ``L 3 2 1``, ``L.3-2-1`` and ``L.3.2.1.`` all normalize to
    the same form.
    """
    s = (clause_id or "").strip().lower()
    s = s.strip(".;:, \t")
    s = _WS_OR_HYPHEN.sub(".", s)
    s = _MULTI_DOT.sub(".", s)
    s = s.strip(".")
    return s


def levenshtein_distance(a: str, b: str, cutoff: int | None = None) -> int:
    """Banded Levenshtein distance. When ``cutoff`` is given, returns
    ``cutoff + 1`` as soon as the distance provably exceeds it."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if cutoff is not None and abs(la - lb) > cutoff:
        return cutoff + 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    previous = list(range(la + 1))
    for j in range(1, lb + 1):
        current = [j] + [0] * la
        row_min = current[0]
        bj = b[j - 1]
        for i in range(1, la + 1):
            cost = 0 if a[i - 1] == bj else 1
            current[i] = min(
                previous[i] + 1,
                current[i - 1] + 1,
                previous[i - 1] + cost,
            )
            if current[i] < row_min:
                row_min = current[i]
        if cutoff is not None and row_min > cutoff:
            return cutoff + 1
        previous = current
    return previous[la]


def fuzzy_contains(excerpt: str, page_text: str, max_ratio: float = 0.10) -> tuple[bool, str]:
    """FR-008 containment check on already-normalized strings.

    Passes when the normalized excerpt is a substring of the normalized page
    text, or when a page window aligned by the longest common blocks matches
    with Levenshtein distance ratio <= ``max_ratio`` (absorbs OCR noise).
    Anchor-based alignment: exact-stepped scans miss optimal alignment, and
    with a <=10% error budget the correct window always shares a >=6-char
    common block with the excerpt. Returns (passed, reason).
    """
    if not excerpt:
        return False, "empty excerpt"
    if not page_text:
        return False, "no text on cited page"
    if excerpt in page_text:
        return True, "substring"
    import difflib

    cutoff = max(1, int(len(excerpt) * max_ratio))
    matcher = difflib.SequenceMatcher(a=excerpt, b=page_text, autojunk=False)
    blocks = sorted(matcher.get_matching_blocks(), key=lambda b: b.size, reverse=True)
    min_anchor = max(2, min(6, len(excerpt) // 2))
    starts: set[int] = set()
    for block in blocks[:8]:
        if block.size < min_anchor:
            continue
        anchor = block.b - block.a
        for offset in range(-4, 5):
            start = anchor + offset
            if 0 <= start < len(page_text):
                starts.add(start)
    best: float | None = None
    for start in starts:
        window = page_text[start:start + len(excerpt)]
        dist = levenshtein_distance(excerpt, window, cutoff=cutoff)
        ratio = dist / max(1, len(excerpt))
        if ratio <= max_ratio:
            return True, f"fuzzy ratio={ratio:.3f}"
        if best is None or ratio < best:
            best = ratio
    if best is not None:
        return False, f"best fuzzy ratio {best:.3f} exceeds {max_ratio}"
    return False, "no common anchor block"
