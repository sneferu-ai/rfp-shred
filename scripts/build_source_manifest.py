"""Build a deterministic content manifest for the clean showcase source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "SOURCE_MANIFEST.json"

EXCLUDED_DIRS = {
    ".cache",
    ".demo",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "demo-files",
    "dist",
    "files",
    "htmlcov",
    "venv",
}
EXCLUDED_FILES = {
    ".coverage",
    ".env",
    "FINAL_PRODUCT.json",
    "SOURCE_MANIFEST.json",
    "demo.sqlite3",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return (
        path.is_file()
        and relative.name not in EXCLUDED_FILES
        and not any(part in EXCLUDED_DIRS for part in relative.parts)
    )


def main() -> None:
    entries = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(ROOT.rglob("*"))
        if _included(path)
    ]
    tree = hashlib.sha256()
    for entry in entries:
        tree.update(entry["path"].encode("utf-8"))
        tree.update(b"\0")
        tree.update(entry["sha256"].encode("ascii"))
        tree.update(b"\n")
    manifest = {
        "schema": "rfp-shred-source-manifest-v1",
        "algorithm": "sha256(path + NUL + file_sha256 + LF), paths sorted",
        "file_count": len(entries),
        "tree_sha256": tree.hexdigest(),
        "files": entries,
    }
    OUTPUT.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"{manifest['tree_sha256']}  {len(entries)} files")


if __name__ == "__main__":
    main()
