"""Founder watchlist matching (FR-022).

Postings where the NAICS sets intersect the founder's ``watch_naics`` AND
the posting's set-aside appears in the founder's ``watch_set_asides`` match.
An empty set-aside watchlist matches any set-aside (the founder watches
everything); an empty NAICS watchlist matches nothing (the founder has not
told us what they pursue).
"""

from __future__ import annotations

from app.sweep.client import SamNoticeData


def matches_watchlist(notice: SamNoticeData, watch_naics: list[str], watch_set_asides: list[str]) -> bool:
    if not watch_naics:
        return False
    if not set(notice.naics) & set(str(n) for n in watch_naics):
        return False
    if not watch_set_asides:
        return True
    return (notice.set_aside or "") in set(watch_set_asides)
