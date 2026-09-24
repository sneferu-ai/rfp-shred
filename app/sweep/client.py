"""SAM.gov opportunities API client (FR-021).

- GET https://api.sam.gov/opportunities/v2/search with postedFrom/postedTo,
  limit=1000, offset pagination until a short page
- client-side throttle at SAM_MAX_RPS
- every call individually wrapped: a dead call is recorded and the sweep
  continues to completion
- the parser tolerates renamed/extra fields: unrecognized keys are logged as
  structured warnings and parsing continues with recognized keys (OBL-7)
- attachment fetch validates size + header signature exactly like FR-004
  intake (OBL-31/54)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.pipeline.intake_files import sniff_type

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
PAGE_LIMIT = 1000

KNOWN_NOTICE_KEYS = {
    "noticeId", "title", "solicitationNumber", "department", "subTier",
    "office", "postedDate", "type", "baseType", "archiveType", "archiveDate",
    "setAside", "setAsideDescription", "responseDeadLine", "naics",
    "classificationCode", "uiLink", "active", "resourceLinks", "description",
    "organizationType", "officeAddress", "placeOfPerformance", "pointOfContact",
    "additionalInfoLink", "naicsCodes", "responseUrl",
}


@dataclass
class SamNoticeData:
    notice_id: str
    title: str
    naics: list[str]
    set_aside: str | None
    posted_at: datetime | None
    due_at: datetime | None
    payload: dict[str, Any]
    attachment_url: str | None = None


@dataclass
class FetchResult:
    notices: list[SamNoticeData] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_notice(raw: dict[str, Any], errors: list[dict[str, Any]]) -> SamNoticeData | None:
    """Tolerant parser: unrecognized keys -> structured warning, recognized
    keys still parsed (AC-018)."""
    unknown = sorted(set(raw) - KNOWN_NOTICE_KEYS)
    if unknown:
        errors.append(
            {"kind": "unrecognized_field", "fields": unknown, "notice_id": raw.get("noticeId")}
        )
    notice_id = raw.get("noticeId")
    if not notice_id:
        errors.append({"kind": "missing_notice_id", "keys": sorted(raw)[:10]})
        return None
    naics_raw = raw.get("naics") or raw.get("naicsCodes") or []
    if isinstance(naics_raw, (str, int)):
        naics = [str(naics_raw)]
    else:
        naics = [str(n) for n in naics_raw if n]
    links = raw.get("resourceLinks") or []
    attachment_url = raw.get("responseUrl") or (links[0] if links else None) or raw.get("uiLink")
    return SamNoticeData(
        notice_id=str(notice_id),
        title=str(raw.get("title") or ""),
        naics=naics,
        set_aside=raw.get("setAside"),
        posted_at=_parse_dt(raw.get("postedDate")),
        due_at=_parse_dt(raw.get("responseDeadLine")),
        payload=raw,
        attachment_url=attachment_url,
    )


class RateThrottle:
    """Client-side throttle at SAM_MAX_RPS (sleep injectable for tests)."""

    def __init__(self, max_rps: float, sleep_fn=time.sleep) -> None:
        self.min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._last = 0.0
        self._sleep = sleep_fn
        self._now = time.monotonic

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._now()
        delta = now - self._last
        if delta < self.min_interval:
            self._sleep(self.min_interval - delta)
        self._last = self._now()


class SamGovClient:
    def __init__(
        self,
        api_key: str,
        *,
        max_rps: float = 2.0,
        base_url: str = SEARCH_URL,
        transport=None,
        sleep_fn=time.sleep,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.throttle = RateThrottle(max_rps, sleep_fn)
        self._transport = transport

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        self.throttle.wait()
        with httpx.Client(transport=self._transport, timeout=60.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    def _get_bytes(self, url: str) -> bytes:
        import httpx

        self.throttle.wait()
        with httpx.Client(transport=self._transport, timeout=120.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content

    def fetch_postings(self, posted_from: str, posted_to: str) -> FetchResult:
        """Paginate the trailing window. Per-call isolation: a failed call
        appends to errors and the sweep continues (FR-021)."""
        result = FetchResult()
        offset = 0
        while True:
            params = {
                "api_key": self.api_key,
                "postedFrom": posted_from,
                "postedTo": posted_to,
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            try:
                payload = self._get_json(self.base_url, params)
                result.calls += 1
            except Exception as exc:
                result.errors.append(
                    {"kind": "call_failed", "offset": offset, "error": str(exc)[:300]}
                )
                offset += PAGE_LIMIT
                if offset > 10 * PAGE_LIMIT:  # never spin forever on a dead endpoint
                    break
                continue
            raw_notices = payload.get("opportunitiesData") or []
            for raw in raw_notices:
                notice = parse_notice(raw, result.errors)
                if notice is not None:
                    result.notices.append(notice)
            if len(raw_notices) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        return result

    def fetch_attachment(self, url: str, *, max_bytes: int) -> bytes:
        """FR-022/OBL-31: downloaded bytes are validated against
        MAX_UPLOAD_MB and header-signature checked before processing."""
        data = self._get_bytes(url)
        if len(data) > max_bytes:
            raise AttachmentRejected("oversize_attachment")
        kind = sniff_type(data)
        if kind not in ("pdf", "docx", "zip"):
            raise AttachmentRejected("invalid_attachment_type")
        return data


class AttachmentRejected(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
