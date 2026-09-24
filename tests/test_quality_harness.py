"""FR-025 evaluation harness: metrics math, exit codes, per-subset
reporting, lower-bound labeling (AC-017/030)."""

import json
from pathlib import Path

from app.core.model_client import MockModelClient
from scripts.evaluate_extraction import evaluate_corpus, main, token_overlap
from tests.conftest import make_pdf

GT = {
    "requirements": [
        {"clause_id": "L.1", "section": "Section L", "page": 2,
         "text": "The offeror shall submit a technical volume not to exceed 50 pages.", "is_binding": True},
        {"clause_id": "M.1", "section": "Section M", "page": 3,
         "text": "The Government will evaluate technical approach for soundness.", "is_binding": True},
    ]
}

PAGES = [
    ["Section B", "This section contains the administrative details for the solicitation effort."],
    ["Section L", "L.1 The offeror shall submit a technical volume not to exceed 50 pages."],
    ["Section M", "M.1 The Government will evaluate technical approach for soundness."],
]


def _corpus(tmp_path: Path, gt: dict = GT, pages=PAGES) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    make_pdf(corpus / "r1.pdf", pages)
    (corpus / "r1.json").write_text(json.dumps(gt))
    (corpus / "manifest.json").write_text(json.dumps({"r1": "MOCK-1"}))
    return corpus


def test_token_overlap_math():
    assert token_overlap("the offeror shall submit", "the offeror shall submit") == 1.0
    assert token_overlap("the offeror", "the offeror shall") == 1.0  # smaller side fully covered
    assert token_overlap("alpha beta", "gamma delta") == 0.0


def test_harness_passes_on_matching_corpus(db, settings, tmp_path):
    corpus = _corpus(tmp_path)
    metrics = evaluate_corpus(corpus, settings=settings, model_client=MockModelClient())
    assert metrics["omission_rate"] == 0.0
    assert metrics["fp_rate"] <= 0.5  # mock may surface the admin line
    assert "digital" in metrics["per_format"]


def test_harness_exit_codes(tmp_path, settings, monkeypatch):
    corpus = _corpus(tmp_path)
    # matching GT -> exit 0; results.json written
    rc = main(["--corpus", str(corpus)])
    assert rc == 0
    results = json.loads((corpus / "results.json").read_text())
    assert "LOWER-BOUND" in results["note"]  # omission labeled as lower-bound estimate

    # doctored GT: requirements the document cannot contain -> omission 100%
    bad_gt = {"requirements": [
        {"clause_id": "X.1", "section": "X", "page": 1,
         "text": "The offeror shall deliver seventeen unicorns to Mars.", "is_binding": True},
    ]}
    (corpus / "r1.json").write_text(json.dumps(bad_gt))
    rc = main(["--corpus", str(corpus)])
    assert rc == 1


def test_page_attribution_error_counted(settings, tmp_path):
    corpus = _corpus(tmp_path)
    # GT claims page 99 for L.1 — the row cites page 2 -> page error
    gt = json.loads(json.dumps(GT))
    gt["requirements"][0]["page"] = 99
    (corpus / "r1.json").write_text(json.dumps(gt))
    metrics = evaluate_corpus(corpus, settings=settings, model_client=MockModelClient())
    assert metrics["page_attr_error"] > 0
