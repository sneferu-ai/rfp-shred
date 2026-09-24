"""Transform layer persistence + 20-table schema + CHECK constraints."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.models import (
    ALL_TABLES,
    AuditDrop,
    FilteredLine,
    RawModelOutput,
    Requirement,
    Rfp,
)
from app.pipeline.audit import audit_items
from app.pipeline.filter import split_requirements
from app.pipeline.mine import CallRecord, MineResult, MinedItem
from app.pipeline.transform import persist_mine_result
from tests.conftest import make_account, make_rfp


def test_all_twenty_tables_created(engine):
    present = set(inspect(engine).get_table_names())
    missing = set(ALL_TABLES) - present
    assert not missing, f"missing tables: {missing}"
    assert len(set(ALL_TABLES)) == 20


def test_check_constraint_rejects_invalid_state(db):
    account = make_account(db)
    rfp = Rfp(account_id=account.id, state="bogus-state")
    db.add(rfp)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_check_constraint_rejects_invalid_job_status(db):
    from app.core.models import Job

    job = Job(status="bogus")
    db.add(job)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_jsonb_columns_roundtrip(db):
    account = make_account(db)
    account.watch_naics = ["541511", "541512"]
    db.flush()
    db.expire_all()
    fetched = db.get(type(account), account.id)
    assert fetched.watch_naics == ["541511", "541512"]


def _mine_result() -> MineResult:
    items = [
        MinedItem("L 3 2 1", "Section L", 2, "The offeror shall submit.", "shall submit", True, 0, 0),
        MinedItem("L.3.2.1", "Section L", 1, "The offeror must register.", "must register", True, 0, 1),
        MinedItem("B.1", "Section B", 1, "Table of contents entry.", "contents", False, 0, 2),
        MinedItem("L.9", "Section L", 1, "The offeror shall teleport.", "teleport", True, 0, 3),
    ]
    pages = {
        1: "The offeror must register. Table of contents entry.",
        2: "The offeror shall submit.",
    }
    mine = MineResult(
        items=items,
        calls=[CallRecord(chunk_index=0, raw_json={"requirements": []}, tokens_in=100, tokens_out=50, finish_reason="stop")],
    )
    filtered = split_requirements(mine.items)
    audit = audit_items(list(filtered.requirements), pages)
    return mine, filtered, audit


def test_persist_mine_result(db):
    account = make_account(db)
    rfp = make_rfp(db, account)
    from app.core.models import ExtractionRun

    run = ExtractionRun(rfp_id=rfp.id, stage="auditing")
    db.add(run)
    db.flush()
    mine, filtered, audit = _mine_result()
    outcome = persist_mine_result(db, rfp_id=rfp.id, run=run, mine=mine, audit=audit, filtered=filtered)

    assert outcome.raw_outputs == 1
    assert outcome.requirements == 2  # teleport excerpt failed the audit
    assert outcome.audit_drops == 1
    assert outcome.filtered_lines == 1

    rows = db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq)
    ).scalars().all()
    # deterministic document order: page 1 before page 2
    assert [r.page for r in rows] == [1, 2]
    # clause ids normalized through the transform (AC-049)
    assert {r.clause_id for r in rows} == {"l.3.2.1"}
    # raw output linked back
    raw = db.execute(select(RawModelOutput)).scalars().one()
    assert rows[0].model_output_id == raw.id

    drop = db.execute(select(AuditDrop).where(AuditDrop.rfp_id == rfp.id)).scalars().one()
    assert "not verified" in drop.audit_failure_reason
    assert drop.text == "The offeror shall teleport."

    line = db.execute(select(FilteredLine).where(FilteredLine.rfp_id == rfp.id)).scalars().one()
    assert line.full_text == "Table of contents entry."
    assert line.text_preview == line.full_text[:80]


def test_reprocess_replaces_generated_rows_and_preserves_human_review(db):
    account = make_account(db)
    rfp = make_rfp(db, account)
    from app.core.models import ExtractionRun

    first_run = ExtractionRun(rfp_id=rfp.id, stage="auditing")
    db.add(first_run)
    db.flush()
    mine, filtered, audit = _mine_result()
    persist_mine_result(
        db, rfp_id=rfp.id, run=first_run, mine=mine, audit=audit, filtered=filtered
    )
    first_rows = db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq)
    ).scalars().all()
    reviewed = first_rows[0]
    reviewed.response = "Use proposal volume II"
    reviewed.tags = ["technical"]
    reviewed.verified_at = datetime.now(timezone.utc)
    reviewed_id = reviewed.id

    second_run = ExtractionRun(rfp_id=rfp.id, stage="auditing", attempt=2)
    db.add(second_run)
    db.flush()
    mine2, filtered2, audit2 = _mine_result()
    # Simulate overlap repeating an already-audited requirement.
    mine2.items.insert(1, mine2.items[0])
    filtered2 = split_requirements(mine2.items)
    pages = {
        1: "The offeror must register. Table of contents entry.",
        2: "The offeror shall submit.",
    }
    audit2 = audit_items(list(filtered2.requirements), pages)
    persist_mine_result(
        db,
        rfp_id=rfp.id,
        run=second_run,
        mine=mine2,
        audit=audit2,
        filtered=filtered2,
    )

    rows = db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq)
    ).scalars().all()
    assert len(rows) == 2
    assert [row.seq for row in rows] == [1, 2]
    preserved = db.get(Requirement, reviewed_id)
    assert preserved is not None
    assert preserved.response == "Use proposal volume II"
    assert preserved.tags == ["technical"]
    assert preserved.verified_at is not None
    assert len(db.execute(select(FilteredLine).where(FilteredLine.rfp_id == rfp.id)).scalars().all()) == 1
    assert len(db.execute(select(AuditDrop).where(AuditDrop.rfp_id == rfp.id)).scalars().all()) == 1


def test_rfp_delete_cascades_children(db):
    account = make_account(db)
    rfp = make_rfp(db, account)
    from tests.conftest import add_requirements

    add_requirements(db, rfp, 3)
    db.commit()
    db.delete(rfp)
    db.commit()
    remaining = db.execute(select(Requirement).where(Requirement.rfp_id == rfp.id)).scalars().all()
    assert remaining == []
