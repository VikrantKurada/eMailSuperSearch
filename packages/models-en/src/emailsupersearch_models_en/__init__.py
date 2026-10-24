"""English embedding model data: int8 ONNX all-MiniLM-L6-v2 plus its tokenizer."""

from pathlib import Path

EMBEDDER_ID = "minilm-l6-v2-int8"
EMBEDDING_DIM = 384


def embedding_dir() -> Path:
    """Return the directory holding model.onnx and tokenizer.json."""
    return Path(__file__).parent / "data"
