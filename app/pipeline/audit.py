"""Citation audit (FR-008).

Each mined row's excerpt AND exported requirement text must verify against the
cited page after normalization: substring containment OR Levenshtein ratio
<= 0.10 (fuzzy match absorbing OCR noise). Checking only the short excerpt
would allow a model to attach fabricated body text to a real quotation.
Failures are never silently discarded — they are returned for persistence to
``audit_drops`` and surfaced on S6 (FR-040).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.mine import MinedItem
from app.pipeline.text_utils import fuzzy_contains, normalize_text

MAX_RATIO = 0.10


@dataclass
class AuditFailure:
    item: MinedItem
    reason: str


@dataclass
class AuditOutcome:
    passed: list[MinedItem]
    failures: list[AuditFailure]


def _verification_failure(
    item: MinedItem, page_text: str, *, max_ratio: float
) -> str | None:
    checks = (("excerpt", item.excerpt), ("requirement text", item.text))
    for label, candidate in checks:
        normalized = normalize_text(candidate)
        if not normalized:
            return f"{label} was empty"
        ok, detail = fuzzy_contains(normalized, page_text, max_ratio=max_ratio)
        if not ok:
            return f"{label} not verified on page {item.page}: {detail}"
    return None


def locate_source_pages(
    items: list[MinedItem], pages: dict[int, str], *, max_ratio: float = MAX_RATIO
) -> list[MinedItem]:
    """Correct a model page using unique, independently verified source text.

    Raw model output retains the claimed page. The transformed row may move
    only when both its excerpt and full body verify on exactly one source page;
    ambiguous repeated text stays on the claimed page and the normal audit
    decides whether it is acceptable.
    """
    normalized_pages = {page: normalize_text(text) for page, text in pages.items()}
    for item in items:
        matches = [
            page
            for page, page_text in normalized_pages.items()
            if _verification_failure(item, page_text, max_ratio=max_ratio) is None
        ]
        if len(matches) == 1:
            item.page = matches[0]
    return items


def audit_items(
    items: list[MinedItem],
    pages: dict[int, str],
    *,
    max_ratio: float = MAX_RATIO,
) -> AuditOutcome:
    normalized_pages = {p: normalize_text(t) for p, t in pages.items()}
    passed: list[MinedItem] = []
    failures: list[AuditFailure] = []
    for item in items:
        if item.page not in normalized_pages:
            failures.append(
                AuditFailure(item=item, reason=f"cited page {item.page} outside document range")
            )
            continue
        page_text = normalized_pages[item.page]
        failure = _verification_failure(item, page_text, max_ratio=max_ratio)
        if failure is None:
            passed.append(item)
        else:
            failures.append(AuditFailure(item=item, reason=failure))
    return AuditOutcome(passed=passed, failures=failures)
