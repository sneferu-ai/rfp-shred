"""The committed smoke fixture proves itself: the pipeline extracts the
clauses smoke.sh asserts on (AC-020's content verification, offline half)."""

from pathlib import Path

from app.core.model_client import MockModelClient
from app.pipeline.chunking import build_chunks
from app.pipeline.mine import mine_chunks
from app.pipeline.pagemap import build_page_map
from scripts.download_corpus import generate_mock_corpus

FIXTURE = Path(__file__).parent / "fixtures" / "corpus" / "smoke_fixture.pdf"


def test_smoke_fixture_extracts_expected_clauses():
    assert FIXTURE.exists(), "smoke fixture must be committed"
    page_map = build_page_map(FIXTURE)
    assert page_map.page_count == 5
    chunks = build_chunks(page_map.pages)
    result = mine_chunks(MockModelClient(), chunks, sleep_fn=lambda s: None)
    clauses = {i.clause_id for i in result.items if i.is_requirement}
    assert {"L.1", "L.2", "M.1"} <= clauses
    assert any("shall" in i.text.lower() for i in result.items)


def test_mock_corpus_generator(tmp_path):
    corpus = tmp_path / "corpus"
    assert generate_mock_corpus(corpus) == 0
    import json

    manifest = json.loads((corpus / "manifest.json").read_text())
    assert len(manifest) == 15  # 10 PDFs + 5 DOCX
    pdfs = list(corpus.glob("mock-*.pdf"))
    docxs = list(corpus.glob("mock-docx-*.docx"))
    assert len(pdfs) == 10 and len(docxs) == 5
    # generated PDFs parse and contain the known content
    page_map = build_page_map(pdfs[0])
    assert page_map.page_count == 5
    assert any("Section L" in text for text in page_map.pages.values())
    # generated DOCX is a valid zip with the document part
    import zipfile

    with zipfile.ZipFile(docxs[0]) as zf:
        assert "word/document.xml" in zf.namelist()
