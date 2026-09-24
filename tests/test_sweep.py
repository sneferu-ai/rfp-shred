"""FR-021/022/044 sweep: resilience, throttle, parser robustness, watchlist
matching, per-run cap, attachment validation, digest (AC-018/052/053)."""

import json
from pathlib import Path

import httpx

from app.core.models import Account, Job, Rfp, SamNotice, SweepRun
from app.sweep.client import AttachmentRejected, RateThrottle, SamGovClient, parse_notice
from app.sweep.runner import run_sweep_once
from tests.conftest import make_account, pdf_bytes, refresh_db

SMALL_PDF = pdf_bytes([["Section L", "L.1 The offeror shall submit a volume."]])


def _posting(notice_id, naics="541511", set_aside="SDVOSB", title="IT Support", extra=None, attach="http://sam.test/att.pdf"):
    payload = {
        "noticeId": notice_id,
        "title": title,
        "solicitationNumber": f"SOL-{notice_id}",
        "naics": [naics],
        "setAside": set_aside,
        "postedDate": "07/25/2026",
        "responseDeadLine": "08/15/2026",
        "resourceLinks": [attach] if attach else [],
    }
    if extra:
        payload.update(extra)
    return payload


def _stub_client(settings, *, max_rps=1000.0, oversize_one=False, invalid_one=False, fail_first_page=False, renamed_key=False):
    postings = [_posting(f"n{i}") for i in range(1, 6)]
    if renamed_key:
        renamed = _posting("n6", attach=None)
        del renamed["naics"]
        renamed["naics_codes"] = ["541511"]
        renamed["agencyContact"] = "jsmith@agency.gov"  # extra field, tolerated
        postings.append(renamed)

    calls = {"search": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search" in url:
            calls["search"] += 1
            offset = request.url.params.get("offset", "0")
            if fail_first_page and calls["search"] == 1:
                return httpx.Response(500, text="server on fire")
            # when the first page died, postings arrive on the retry's page 2
            data = postings if (offset == "0" if not fail_first_page else offset == "1000") else []
            return httpx.Response(200, json={"opportunitiesData": data, "totalRecords": len(data)})
        if "att.pdf" in url:
            return httpx.Response(200, content=SMALL_PDF)
        if "oversize" in url:
            return httpx.Response(200, content=b"\x00" * (settings.max_upload_bytes + 1))
        if "invalid" in url:
            return httpx.Response(200, content=b"MZ\x90\x00 not a document")
        return httpx.Response(404)

    client = SamGovClient(
        "TESTKEY", max_rps=max_rps, transport=httpx.MockTransport(handler), sleep_fn=lambda s: None
    )
    return client


def _staff_with_watchlist(db):
    founder = make_account(db, staff=True)
    founder.watch_naics = ["541511"]
    founder.watch_set_asides = ["SDVOSB"]
    db.commit()
    return founder


def test_parse_notice_unrecognized_fields_warn_but_parse():
    errors: list[dict] = []
    notice = parse_notice(_posting("x1", extra={"weirdNewField": 1}), errors)
    assert notice is not None
    assert any(e["kind"] == "unrecognized_field" for e in errors)
    assert notice.notice_id == "x1"
    assert notice.naics == ["541511"]


def test_throttle_sleeps_between_calls():
    slept: list[float] = []
    now = {"t": 0.0}
    throttle = RateThrottle(2.0, sleep_fn=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)))
    throttle._now = lambda: now["t"]
    throttle.wait()  # first call: delta 0 -> sleeps one interval
    throttle.wait()  # second call: still no elapsed time -> sleeps again
    assert len(slept) == 2
    assert all(abs(s - 0.5) < 0.01 for s in slept)
    now["t"] += 1.0  # interval elapsed: no more sleeping
    throttle.wait()
    assert len(slept) == 2


def test_sweep_end_to_end(db, settings):
    founder = _staff_with_watchlist(db)
    client = _stub_client(settings, fail_first_page=True, renamed_key=True)
    sweep = run_sweep_once(db, settings, client=client)
    db.commit()

    assert sweep.found == 6
    assert any(e["kind"] == "call_failed" for e in sweep.errors)
    assert any(e["kind"] == "unrecognized_field" for e in sweep.errors)
    # 5 postings have valid attachments + matching watchlist -> 5 matches
    rfps = db.query(Rfp).filter(Rfp.origin == "sweep").all()
    assert len(rfps) == 5
    assert all(r.account_id == founder.id for r in rfps)
    assert all(r.retain_source is False for r in rfps)
    jobs = db.query(Job).all()
    assert len(jobs) == 5 and all(j.priority == 1 for j in jobs)
    # notices upserted (renamed-key posting stored with empty naics)
    assert db.query(SamNotice).count() == 6
    # digest email sent to the founder (AC-052)
    outbox = Path(settings.files_dir) / "outbox"
    mails = list(outbox.glob("*.eml"))
    assert mails, "expected a digest email"
    assert "[RFP Shred] 5 new watchlist match(es)" in mails[-1].read_text()


def test_sweep_dedup_on_second_run(db, settings):
    _staff_with_watchlist(db)
    client = _stub_client(settings)
    run_sweep_once(db, settings, client=client)
    db.commit()
    run_sweep_once(db, settings, client=_stub_client(settings))
    db.commit()
    assert db.query(Rfp).filter(Rfp.origin == "sweep").count() == 5


def test_sweep_per_run_cap_logs_excess(db, settings):
    _staff_with_watchlist(db)
    settings.max_sweep_matches_per_run = 2
    sweep = run_sweep_once(db, settings, client=_stub_client(settings))
    db.commit()
    assert db.query(Rfp).filter(Rfp.origin == "sweep").count() == 2
    excess = [e for e in sweep.errors if e["kind"] == "excess_matches_skipped"]
    assert excess and excess[0]["count"] == 3


def test_sweep_attachment_validation(db, settings):
    """AC-053: oversize + invalid-type attachments logged and skipped."""
    _staff_with_watchlist(db)
    postings = [
        _posting("big", attach="http://sam.test/oversize"),
        _posting("weird", attach="http://sam.test/invalid"),
        _posting("good"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search" in url:
            data = postings if request.url.params.get("offset", "0") == "0" else []
            return httpx.Response(200, json={"opportunitiesData": data})
        if "oversize" in url:
            return httpx.Response(200, content=b"\x00" * (settings.max_upload_bytes + 1))
        if "invalid" in url:
            return httpx.Response(200, content=b"MZ not a document")
        if "att.pdf" in url:
            return httpx.Response(200, content=SMALL_PDF)
        return httpx.Response(404)

    client = SamGovClient("K", max_rps=1000.0, transport=httpx.MockTransport(handler), sleep_fn=lambda s: None)
    sweep = run_sweep_once(db, settings, client=client)
    db.commit()
    kinds = {e["kind"] for e in sweep.errors}
    assert "oversize_attachment" in kinds
    assert "invalid_attachment_type" in kinds
    rfps = db.query(Rfp).filter(Rfp.origin == "sweep").all()
    assert len(rfps) == 1  # only the valid sibling
    assert rfps[0].solicitation_no == "SOL-good"


def test_sweep_zero_matches_sends_no_email(db, settings):
    founder = _staff_with_watchlist(db)
    founder.watch_naics = ["999999"]
    db.commit()
    sweep = run_sweep_once(db, settings, client=_stub_client(settings))
    db.commit()
    assert sweep.matched == 0
    outbox = Path(settings.files_dir) / "outbox"
    assert not outbox.exists() or list(outbox.glob("*.eml")) == []
