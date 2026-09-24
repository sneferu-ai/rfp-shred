"""Transform layer (FR-007/FR-009): raw model output -> persisted rows.

Maps validated model JSON to ``requirements`` (citation-verified binding
items), ``filtered_lines`` (noise drops, full text preserved), and
``audit_drops`` (citation failures), with ``clause_id`` normalization and a
``requirements.model_output_id`` back-reference. Raw output is persisted to
``raw_model_outputs`` BEFORE transformation so prompts can iterate without
schema churn.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import (
    AuditDrop,
    ExtractionRun,
    FilteredLine,
    RawModelOutput,
    Requirement,
)
from app.pipeline.audit import AuditOutcome
from app.pipeline.filter import FilterOutcome
from app.pipeline.mine import CallRecord, MineResult
from app.pipeline.text_utils import normalize_clause_id, normalize_text


@dataclass
class PersistOutcome:
    requirements: int
    filtered_lines: int
    audit_drops: int
    raw_outputs: int


def _item_key(item) -> tuple[str, int, str]:
    return (
        normalize_clause_id(item.clause_id),
        item.page,
        normalize_text(item.text),
    )


def _requirement_key(row: Requirement) -> tuple[str, int, str]:
    return (normalize_clause_id(row.clause_id), row.page, normalize_text(row.body))


def persist_mine_result(
    session: Session,
    *,
    rfp_id: uuid.UUID,
    run: ExtractionRun,
    mine: MineResult,
    audit: AuditOutcome,
    filtered: FilterOutcome,
) -> PersistOutcome:
    # 1. raw outputs first
    output_ids: dict[int, uuid.UUID] = {}
    for call in mine.calls:
        row = RawModelOutput(
            extraction_run_id=run.id,
            chunk_index=call.chunk_index,
            raw_json=call.raw_json,
            tokens_in=call.tokens_in,
            tokens_out=call.tokens_out,
            finish_reason=call.finish_reason,
            prompt_version=call.prompt_version,
        )
        session.add(row)
        session.flush()
        output_ids[call.chunk_index] = row.id

    # 2. Replace the prior generated view only after mining/audit succeeded.
    # Exact requirement matches are updated in place so a safe reprocess keeps
    # human responses, tags, verification, corrections, and stable row IDs.
    # This occurs in the same DB transaction as persistence: any later error
    # rolls the replacement back rather than publishing a half-new matrix.
    existing = session.execute(
        select(Requirement).where(Requirement.rfp_id == rfp_id)
    ).scalars().all()
    existing_by_key: dict[tuple[str, int, str], list[Requirement]] = {}
    for row in existing:
        existing_by_key.setdefault(_requirement_key(row), []).append(row)

    for row in session.execute(
        select(FilteredLine).where(FilteredLine.rfp_id == rfp_id)
    ).scalars().all():
        session.delete(row)
    for row in session.execute(
        select(AuditDrop).where(AuditDrop.rfp_id == rfp_id)
    ).scalars().all():
        session.delete(row)

    # Citation-verified binding requirements in deterministic document order.
    # Chunk overlap and recursive halving can repeat the same model item; exact
    # normalized duplicates become one matrix row.
    surviving = []
    seen_keys: set[tuple[str, int, str]] = set()
    for item in sorted(audit.passed, key=lambda i: (i.page, i.doc_order)):
        key = _item_key(item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        surviving.append(item)

    retained_ids: set[uuid.UUID] = set()
    seq = 0
    for item in surviving:
        seq += 1
        key = _item_key(item)
        candidates = existing_by_key.get(key, [])
        row = candidates.pop(0) if candidates else None
        if row is None:
            row = Requirement(
                rfp_id=rfp_id,
                seq=seq,
                clause_id=normalize_clause_id(item.clause_id),
                section=item.section,
                page=item.page,
                body=item.text,
                excerpt=item.excerpt,
                tags=[],
                model_output_id=output_ids.get(item.chunk_index),
            )
            session.add(row)
        else:
            retained_ids.add(row.id)
            row.seq = seq
            row.clause_id = normalize_clause_id(item.clause_id)
            row.section = item.section
            row.page = item.page
            row.body = item.text
            row.excerpt = item.excerpt
            row.model_output_id = output_ids.get(item.chunk_index)

    # Operator-restored rows have no model output. Preserve an unmatched one
    # across reprocessing instead of silently erasing a human decision.
    for row in existing:
        if row.id in retained_ids:
            continue
        if row.section == "(restored)" and _requirement_key(row) not in seen_keys:
            seq += 1
            row.seq = seq
            retained_ids.add(row.id)
            continue
        session.delete(row)

    # 3. citation failures -> audit_drops (FR-008, never silent)
    audit_seq = 0
    for failure in sorted(audit.failures, key=lambda f: (f.item.page, f.item.doc_order)):
        audit_seq += 1
        session.add(
            AuditDrop(
                rfp_id=rfp_id,
                seq=audit_seq,
                page=failure.item.page,
                excerpt=failure.item.excerpt,
                text=failure.item.text,
                audit_failure_reason=failure.reason,
            )
        )

    # 4. noise-filter drops -> filtered_lines (FR-009, full text preserved)
    filt_seq = 0
    for item, reason in sorted(filtered.filtered, key=lambda t: (t[0].page, t[0].doc_order)):
        filt_seq += 1
        session.add(
            FilteredLine(
                rfp_id=rfp_id,
                seq=filt_seq,
                page=item.page,
                text_preview=item.text[:80],
                full_text=item.text,
                filter_reason=reason,
            )
        )

    session.flush()
    return PersistOutcome(
        requirements=seq,
        filtered_lines=filt_seq,
        audit_drops=audit_seq,
        raw_outputs=len(mine.calls),
    )
