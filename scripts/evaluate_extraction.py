"""FR-025 evaluation harness: ``python -m scripts.evaluate_extraction --corpus <dir>``.

Runs the full pipeline over the annotated corpus and prints:
- omission rate  = GT binding requirements not matched by any extracted row
  at >=80% token overlap / total GT binding requirements
- false-positive rate = extracted rows matching no GT requirement at >=80%
  token overlap / total extracted rows
- page-attribution error rate = matched rows where |cited - GT page| > 1 /
  total matched rows
Per-subset metrics (digital vs. scanned vs. DOCX) are reported; the
thresholds are aggregate (FR-025 justification). The omission rate is a
LOWER-BOUND estimate — founder annotations may miss requirements the model
also missed; completeness validation requires a second-pass expert review.
Exit 1 when omission > 10%, FP > 20%, or page-attribution error > 5%.

Token overlap is defined as |intersection| / min(|a|, |b|) over normalized
tokens — a match requires >=0.8 of the smaller side covered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.model_client import get_model_client
from app.pipeline.chunking import build_chunks
from app.pipeline.mine import mine_chunks
from app.pipeline.pagemap import build_page_map
from app.pipeline.text_utils import normalize_text

OMISSION_THRESHOLD = 0.10
FP_THRESHOLD = 0.20
PAGE_ATTR_THRESHOLD = 0.05
MATCH_OVERLAP = 0.80

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def load_manifest(corpus_dir: str | Path) -> dict[str, str]:
    path = Path(corpus_dir) / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_ground_truth(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data.get("requirements", [])


def token_set(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(normalize_text(text)))


def token_overlap(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _classify(rfp_id: str, corpus: Path, ocr_flagged: list[int]) -> str:
    for ext in (".docx",):
        if (corpus / f"{rfp_id}{ext}").exists():
            return "docx"
    if ocr_flagged:
        return "scanned"
    return "digital"


def _extract_for_document(doc_path: Path, settings: Settings, model_client=None) -> tuple[list[dict], list[int]]:
    """Run the real pipeline pieces (pagemap -> chunk -> mine) and return
    extracted rows as dicts. Citation audit is not applied here: the harness
    measures extraction recall/precision of the mining step against ground
    truth, identical to FR-025's definitions."""
    client = model_client or get_model_client(settings)
    page_map = build_page_map(doc_path)
    chunks = build_chunks(page_map.pages)
    result = mine_chunks(client, chunks, max_tokens=settings.model_max_tokens, sleep_fn=lambda s: None)
    return (
        [{"clause_id": i.clause_id, "section": i.section, "page": i.page, "text": i.text}
         for i in result.items if i.is_requirement],
        page_map.ocr_flagged,
    )


def _score_subset(rows: list[dict], ground_truth: list[dict]) -> dict:
    binding_gt = [g for g in ground_truth if g.get("is_binding", True)]
    matched_gt: set[int] = set()
    matched_rows = 0
    page_errors = 0
    for row in rows:
        best_idx, best_overlap = None, 0.0
        for idx, gt in enumerate(ground_truth):
            overlap = token_overlap(row["text"], gt.get("text", ""))
            if overlap > best_overlap:
                best_idx, best_overlap = idx, overlap
        if best_idx is not None and best_overlap >= MATCH_OVERLAP:
            matched_rows += 1
            gt = ground_truth[best_idx]
            if gt.get("is_binding", True):
                matched_gt.add(best_idx)
            try:
                if abs(int(row["page"]) - int(gt.get("page", 0))) > 1:
                    page_errors += 1
            except (TypeError, ValueError):
                page_errors += 1
    omission = 1.0 - (len(matched_gt) / len(binding_gt)) if binding_gt else 0.0
    fp = 1.0 - (matched_rows / len(rows)) if rows else 0.0
    page_attr = (page_errors / matched_rows) if matched_rows else 0.0
    return {
        "ground_truth_binding": len(binding_gt),
        "extracted": len(rows),
        "matched": matched_rows,
        "omission_rate": round(omission, 4),
        "fp_rate": round(fp, 4),
        "page_attr_error": round(page_attr, 4),
    }


def evaluate_corpus(
    corpus_dir: str | Path,
    *,
    subset: list[str] | None = None,
    settings: Settings | None = None,
    model_client=None,
) -> dict | None:
    corpus = Path(corpus_dir)
    manifest = load_manifest(corpus)
    rfp_ids = subset or sorted(manifest)
    if not rfp_ids:
        return None
    settings = settings or Settings.from_env(strict=False)

    per_format: dict[str, dict] = {}
    totals = {"gt": 0, "extracted": 0, "matched": 0, "matched_gt": 0, "page_errors": 0}
    results: dict[str, dict] = {}
    for rfp_id in rfp_ids:
        gt_path = corpus / f"{rfp_id}.json"
        if not gt_path.exists():
            continue
        ground_truth = load_ground_truth(gt_path)
        doc_path = None
        for ext in (".pdf", ".docx"):
            candidate = corpus / f"{rfp_id}{ext}"
            if candidate.exists():
                doc_path = candidate
                break
        if doc_path is None:
            continue
        rows, ocr_flagged = _extract_for_document(doc_path, settings, model_client)
        fmt = _classify(rfp_id, corpus, ocr_flagged)
        scores = _score_subset(rows, ground_truth)
        results[rfp_id] = {"format": fmt, **scores}
        binding = [g for g in ground_truth if g.get("is_binding", True)]
        doc_page_errors = int(round(scores["page_attr_error"] * scores["matched"]))
        doc_matched_gt = int(round((1 - scores["omission_rate"]) * len(binding)))
        bucket = per_format.setdefault(fmt, {"gt": 0, "extracted": 0, "matched": 0, "matched_gt": 0, "page_errors": 0, "gt_binding_total": 0})
        for acc in (bucket, totals):
            acc["gt"] += scores["ground_truth_binding"]
            acc["extracted"] += scores["extracted"]
            acc["matched"] += scores["matched"]
            acc["matched_gt"] += doc_matched_gt
            acc["page_errors"] += doc_page_errors
        bucket["gt_binding_total"] += len(binding)

    for fmt, b in per_format.items():
        b["omission_rate"] = round(1.0 - (b["matched_gt"] / b["gt_binding_total"]), 4) if b["gt_binding_total"] else 0.0
        b["fp_rate"] = round(1.0 - (b["matched"] / b["extracted"]), 4) if b["extracted"] else 0.0
        b["page_attr_error"] = round(b["page_errors"] / b["matched"], 4) if b["matched"] else 0.0

    aggregate = {
        "omission_rate": round(1.0 - (totals["matched_gt"] / totals["gt"]), 4) if totals["gt"] else 0.0,
        "fp_rate": round(1.0 - (totals["matched"] / totals["extracted"]), 4) if totals["extracted"] else 0.0,
        "page_attr_error": round(totals["page_errors"] / totals["matched"], 4) if totals["matched"] else 0.0,
    }
    return {**aggregate, "per_format": per_format, "documents": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate extraction quality against an annotated corpus")
    parser.add_argument("--corpus", required=True, help="corpus directory (manifest.json + <id>.json + documents)")
    args = parser.parse_args(argv)
    metrics = evaluate_corpus(args.corpus)
    if metrics is None:
        print("empty corpus — nothing to evaluate", file=sys.stderr)
        return 1
    out = {
        **metrics,
        "note": "omission_rate is a LOWER-BOUND estimate: founder annotations may miss "
                "requirements the model also missed. Completeness validation requires a "
                "second-pass expert review of a subset (>=3 RFPs).",
    }
    print(json.dumps(out, indent=2))
    results_path = Path(args.corpus) / "results.json"
    results_path.write_text(json.dumps(out, indent=2))
    failed = (
        metrics["omission_rate"] > OMISSION_THRESHOLD
        or metrics["fp_rate"] > FP_THRESHOLD
        or metrics["page_attr_error"] > PAGE_ATTR_THRESHOLD
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
