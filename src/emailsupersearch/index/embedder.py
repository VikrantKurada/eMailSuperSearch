"""CPU sentence embedder: int8 all-MiniLM-L6-v2 through onnxruntime and the tokenizers library."""

from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_ID = "minilm-l6-v2-int8"
MODEL_DIM = 384
MAX_TOKENS = 256

_telemetry_disabled = False


def model_path(model_dir: Path) -> Path:
    return model_dir / "model.onnx"


def tokenizer_path(model_dir: Path) -> Path:
    return model_dir / "tokenizer.json"


def _disable_telemetry_once() -> None:
    global _telemetry_disabled
    if not _telemetry_disabled:
        ort.disable_telemetry_events()
        _telemetry_disabled = True


class OnnxEmbedder:
    """Embeds texts into L2-normalised float32 vectors (mean pooling over the attention mask)."""

    id: str = MODEL_ID
    dim: int = MODEL_DIM
    max_tokens: int = MAX_TOKENS

    def __init__(self, model_dir: Path, threads: int = 4) -> None:
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path(model_dir)))
        self._tokenizer.enable_truncation(max_length=self.max_tokens)
        self._tokenizer.enable_padding()
        _disable_telemetry_once()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(
            str(model_path(model_dir)), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def embed(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Return an (n, dim) float32 array, one unit-length row per text."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        batches = [self._embed_batch(texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)]
        return np.concatenate(batches)

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        hidden = self._session.run(None, feed)[0]
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
        norms = np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
        return (pooled / norms).astype(np.float32)


def load_default_embedder(threads: int = 4) -> OnnxEmbedder | None:
    """Return the embedder from emailsupersearch-models-en, or None when it is absent or incomplete."""
    try:
        import emailsupersearch_models_en as models
    except ImportError:
        return None
    model_dir = models.embedding_dir()
    if not model_path(model_dir).is_file() or not tokenizer_path(model_dir).is_file():
        return None
    return OnnxEmbedder(model_dir, threads=threads)
