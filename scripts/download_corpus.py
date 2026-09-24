"""Corpus fetcher: ``python -m scripts.download_corpus [--mock-corpus]``.

Normal mode reads tests/fixtures/corpus/manifest.json ({rfp_id: notice_id})
and downloads the corresponding documents from SAM.gov (requires
SAM_API_KEY). ``--mock-corpus`` generates small synthetic PDF/DOCX fixtures
with known content — no SAM key required.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from app.core.config import Settings

CORPUS_DIR = Path("tests/fixtures/corpus")

MOCK_REQUIREMENTS = [
    {"clause_id": "L.1", "section": "Section L", "page": 2,
     "text": "The offeror shall submit a technical volume not to exceed 50 pages.",
     "is_binding": True},
    {"clause_id": "L.2", "section": "Section L", "page": 2,
     "text": "The offeror must provide resumes for all key personnel named in the proposal.",
     "is_binding": True},
    {"clause_id": "L.3", "section": "Section L", "page": 3,
     "text": "The contractor shall deliver monthly status reports to the contracting officer.",
     "is_binding": True},
    {"clause_id": "M.1", "section": "Section M", "page": 4,
     "text": "The Government will evaluate technical approach for soundness and feasibility.",
     "is_binding": True},
    {"clause_id": "M.2", "section": "Section M", "page": 4,
     "text": "Past performance will be evaluated for recency and relevance.",
     "is_binding": True},
    {"clause_id": "B.1", "section": "Section B", "page": 1,
     "text": "This synopsis is issued for informational purposes only.",
     "is_binding": False},
]


def _mock_pages(rfp_index: int) -> list[list[str]]:
    reqs = MOCK_REQUIREMENTS
    return [
        ["SOLICITATION MOCK-%04d" % rfp_index, "Section B", reqs[5]["text"]],
        ["Section L — Instructions to Offerors", "L.1 " + reqs[0]["text"], "L.2 " + reqs[1]["text"]],
        ["L.3 " + reqs[2]["text"], "Attachment 1: Performance Work Statement"],
        ["Section M — Evaluation Factors", "M.1 " + reqs[3]["text"], "M.2 " + reqs[4]["text"]],
        ["End of solicitation MOCK-%04d" % rfp_index],
    ]


def _write_pdf(path: Path, pages: list[list[str]]) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    for lines in pages:
        text = c.beginText(72, 720)
        for line in lines:
            text.textLine(line)
        c.drawText(text)
        c.showPage()
    c.save()


_DOC_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    + "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{line}</w:t></w:r></w:p>"
        for page in _mock_pages(99) for line in page
    )
    + "<w:sectPr/></w:body></w:document>"
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _write_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", _DOC_XML)


def generate_mock_corpus(corpus_dir: Path = CORPUS_DIR) -> int:
    """10 digital PDFs + 5 DOCX with known content + ground truth JSONs.

    Scanned-subset coverage in dev comes from the Docker OCR toolchain path
    (the pipeline flags no-text pages and counts OCR failures honestly);
    synthetic image-only PDFs are deliberately NOT faked here.
    """
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    ground_truth = {"requirements": MOCK_REQUIREMENTS}
    for i in range(10):
        rfp_id = f"mock-{i + 1:04d}"
        manifest[rfp_id] = f"MOCK-{i + 1:04d}"
        _write_pdf(corpus_dir / f"{rfp_id}.pdf", _mock_pages(i + 1))
        (corpus_dir / f"{rfp_id}.json").write_text(json.dumps(ground_truth, indent=2))
    for i in range(5):
        rfp_id = f"mock-docx-{i + 1:02d}"
        manifest[rfp_id] = f"MOCK-DOCX-{i + 1:02d}"
        _write_docx(corpus_dir / f"{rfp_id}.docx")
        (corpus_dir / f"{rfp_id}.json").write_text(json.dumps(ground_truth, indent=2))
    (corpus_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"mock corpus written to {corpus_dir}: 10 PDFs + 5 DOCX with ground truth")
    return 0


def download_real_corpus(settings: Settings, corpus_dir: Path = CORPUS_DIR) -> int:
    """Fetch documents for manifest {rfp_id: notice_id} pairs from SAM.gov."""
    import httpx

    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 1
    if not settings.sam_api_key:
        print("SAM_API_KEY is required for normal mode (or use --mock-corpus)", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text())
    corpus_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120.0) as client:
        for rfp_id, notice_id in manifest.items():
            if (corpus_dir / f"{rfp_id}.pdf").exists() or (corpus_dir / f"{rfp_id}.docx").exists():
                continue
            try:
                resp = client.get(
                    "https://api.sam.gov/opportunities/v2/search",
                    params={"api_key": settings.sam_api_key, "noticeid": notice_id},
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("opportunitiesData") or []
                links = (items[0].get("resourceLinks") or []) if items else []
                if not links:
                    print(f"{rfp_id}: no resource links for notice {notice_id}", file=sys.stderr)
                    continue
                blob = client.get(links[0]).content
                suffix = ".pdf" if blob[:4] == b"%PDF" else ".docx" if blob[:2] == b"PK" else ".bin"
                (corpus_dir / f"{rfp_id}{suffix}").write_bytes(blob)
                print(f"{rfp_id}: downloaded {len(blob)} bytes")
            except Exception as exc:
                print(f"{rfp_id}: download failed: {exc}", file=sys.stderr)
                return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate tests/fixtures/corpus with binary documents")
    parser.add_argument("--mock-corpus", action="store_true", help="generate synthetic fixtures (no SAM key)")
    args = parser.parse_args(argv)
    if args.mock_corpus:
        return generate_mock_corpus()
    return download_real_corpus(Settings.from_env(strict=False))


if __name__ == "__main__":
    raise SystemExit(main())
