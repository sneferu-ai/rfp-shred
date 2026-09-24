"""End-to-end extraction: real reportlab PDF -> real pypdf page map -> mock
model -> mined rows -> citation audit -> persisted matrix (FR-005..FR-009)."""

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.models import (
    AuditDrop,
    ExtractionRun,
    FilteredLine,
    InferenceLog,
    RawModelOutput,
    Requirement,
    Rfp,
)
from app.pipeline.runner import ExtractionFailed, run_extraction
from app.core.model_client import ModelResponse
from tests.conftest import make_account, make_pdf, make_rfp

PAGES = [
    ["SOLICITATION FA1234-26-T-0001", "Section B", "This synopsis is for informational purposes only."],
    ["Section L — Instructions to Offerors",
     "L.1 The offeror shall submit a technical volume not to exceed 50 pages.",
     "L.2 The offeror must provide resumes for all key personnel."],
    ["Section M — Evaluation Factors",
     "M.1 The Government will evaluate technical approach for soundness.",
     "M.2 The contractor shall deliver monthly status reports."],
]


def _ready_rfp(db, account, path: Path) -> tuple[Rfp, ExtractionRun]:
    rfp = make_rfp(db, account, state="received", stored_path=str(path))
    run = ExtractionRun(rfp_id=rfp.id, stage="reading")
    db.add(run)
    db.flush()
    return rfp, run


def test_full_extraction_over_real_pdf(db, settings, tmp_path):
    account = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", PAGES)
    rfp, run = _ready_rfp(db, account, pdf)
    count = run_extraction(db, settings, rfp, run, sleep_fn=lambda s: None)
    db.commit()

    assert count >= 4
    assert rfp.state == "ready"
    assert run.stage == "ready"
    assert rfp.page_count == 3

    rows = db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq)
    ).scalars().all()
    clauses = {r.clause_id for r in rows}
    assert {"l.1", "l.2", "m.1", "m.2"} <= clauses
    # seq is a clean 1..N ladder in document order
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))
    # page cites are real (the L rows come from page 2)
    l1 = next(r for r in rows if r.clause_id == "l.1")
    assert l1.page == 2
    assert "shall" in l1.body
    # narrative line was routed to filtered_lines with full text
    filtered = db.execute(select(FilteredLine).where(FilteredLine.rfp_id == rfp.id)).scalars().all()
    assert any("informational purposes" in f.full_text for f in filtered)
    # raw model outputs persisted with token counts
    raw = db.execute(select(RawModelOutput).where(RawModelOutput.extraction_run_id == run.id)).scalars().all()
    assert raw and all(r.tokens_in > 0 for r in raw)
    # inference cost logged (FR-039)
    costs = db.execute(select(InferenceLog).where(InferenceLog.rfp_id == rfp.id)).scalars().all()
    assert len(costs) == len(raw)
    assert all(float(c.estimated_cost_usd) >= 0 for c in costs)


def test_zero_requirement_document_lands_ready(db, settings, tmp_path):
    account = make_account(db)
    pdf = make_pdf(tmp_path / "prose.pdf", [["A short memo.", "Nothing binding here."]])
    rfp, run = _ready_rfp(db, account, pdf)
    count = run_extraction(db, settings, rfp, run, sleep_fn=lambda s: None)
    assert count == 0
    assert rfp.state == "ready"  # empty state, not an error (FR-009)
    assert db.execute(select(Requirement).where(Requirement.rfp_id == rfp.id)).scalars().all() == []


def test_unprocessable_document_fails_with_cause(db, settings, tmp_path):
    account = make_account(db)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    rfp = make_rfp(db, account, state="received", stored_path=str(bad))
    run = ExtractionRun(rfp_id=rfp.id, stage="reading")
    db.add(run)
    db.flush()
    try:
        run_extraction(db, settings, rfp, run, sleep_fn=lambda s: None)
        assert False, "expected ExtractionFailed"
    except ExtractionFailed as exc:
        assert exc.cause


def test_failed_model_chunk_refuses_partial_ready_matrix(db, settings, tmp_path):
    class AlwaysMalformed:
        def complete(self, *, system, user, max_tokens, json_schema=None):
            return ModelResponse(text="not-json", tokens_in=4, tokens_out=2)

    account = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", PAGES)
    rfp, run = _ready_rfp(db, account, pdf)

    try:
        run_extraction(
            db,
            settings,
            rfp,
            run,
            model_client=AlwaysMalformed(),
            sleep_fn=lambda _s: None,
        )
        assert False, "expected incomplete extraction to fail closed"
    except ExtractionFailed as exc:
        assert "incomplete" in exc.cause
        assert "no partial compliance matrix" in exc.cause
    assert rfp.state != "ready"
    assert db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id)
    ).scalars().all() == []


def test_retention_ready_timestamp_via_heartbeat(db, settings, tmp_path):
    account = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", PAGES)
    rfp, run = _ready_rfp(db, account, pdf)
    before = datetime.now(timezone.utc)
    run_extraction(db, settings, rfp, run, sleep_fn=lambda s: None)
    # final heartbeat doubles as the ready timestamp (FR-020 grace clock)
    hb = run.heartbeat_at
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    assert hb >= before
