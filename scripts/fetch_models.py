"""Download the embedding model files into the models-en package (developer machine only, never shipped).

First run: prints each file's SHA-256 and size so the hashes can be pasted into EXPECTED_SHA256.
Later runs: skip files whose hash matches, otherwise download and verify (a mismatch deletes the file and exits 1).
Uses only the standard library; huggingface_hub is avoided because HF_HUB_OFFLINE may be set.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

_BASE = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
SOURCES: dict[str, str] = {
    "model.onnx": f"{_BASE}/onnx/model_quint8_avx2.onnx",
    "tokenizer.json": f"{_BASE}/tokenizer.json",
}
EXPECTED_SHA256: dict[str, str | None] = {
    "model.onnx": "b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
TARGET_DIR = Path(__file__).resolve().parent.parent / "packages" / "models-en" / "src" / "emailsupersearch_models_en" / "data"
_CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as out:
        while chunk := response.read(_CHUNK):
            out.write(chunk)


def main() -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    unpinned = False
    for name, url in SOURCES.items():
        dest = TARGET_DIR / name
        expected = EXPECTED_SHA256[name]
        if expected is not None and dest.exists() and _sha256(dest) == expected:
            print(f"{name}: present, hash verified, skipping")
            continue
        print(f"{name}: downloading {url}")
        _download(url, dest)
        actual = _sha256(dest)
        size = dest.stat().st_size
        if expected is None:
            print(f"{name} {actual} {size}")
            unpinned = True
        elif actual != expected:
            dest.unlink()
            print(f"{name}: SHA-256 mismatch (expected {expected}, got {actual}); file deleted", file=sys.stderr)
            return 1
        else:
            print(f"{name}: downloaded, hash verified ({size} bytes)")
    if unpinned:
        print("Paste the hashes above into EXPECTED_SHA256 in scripts/fetch_models.py, then run it again to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
