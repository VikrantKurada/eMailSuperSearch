"""Speed probe for the `bench` command (M0).

Times parsing, embedding and nearest-neighbour search on a synthetic corpus (or the owner's own
folder), so the plan's defaults can be set from measurements taken on the target laptop. The report
holds numbers and notes only, never message text, so it is safe to paste back.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from emailsupersearch.db.connection import connect
from emailsupersearch.db.schema import migrate
from emailsupersearch.diagnostics.corpus import generate, write_text_png
from emailsupersearch.index.embedder import (
    OnnxEmbedder,
    load_default_embedder,
    tokenizer_path,
)
from emailsupersearch.index.vectors import VectorIndex
from emailsupersearch.ingest.chunker import Chunker, context_line_for
from emailsupersearch.ingest.parse import ParseError
from emailsupersearch.ingest.workers import _peak_working_set_bytes, parse_document
from emailsupersearch.types import ParsedDocument

MAIL_SUFFIXES = {".eml", ".msg"}
EMBED_BATCH = 32
KNN_QUERIES = 50
KNN_K = 20
PAD_BATCH = 10_000
MB = 1024 * 1024
NO_MODELS_NOTE = "embedder: models package not installed; semantic search disabled"
NO_OCR_NOTE = "ocr: engine module not available in this build"
NO_OCR_ENGINE_NOTE = "ocr: no OCR engine available"
OCR_BENCH_PAGES = 5


@dataclass
class BenchReport:
    parse_files_per_s: float
    embed_chunks_per_s: float | None
    ocr_pages_per_s: float | None
    knn_ms_p50: float | None
    knn_ms_p95: float | None
    knn_vectors: int
    peak_rss_mb: float
    n_files: int
    n_chunks: int
    notes: list[str] = field(default_factory=list)


def _mail_files(corpus_dir: Path) -> list[Path]:
    return sorted(p for p in corpus_dir.rglob("*") if p.is_file() and p.suffix.lower() in MAIL_SUFFIXES)


def _parse_all(files: list[Path], notes: list[str]) -> tuple[list[ParsedDocument], float]:
    """Parse every file one after another in this process; returns the documents and the seconds taken."""
    docs: list[ParsedDocument] = []
    failed = 0
    started = time.perf_counter()
    for path in files:
        try:
            docs.append(parse_document(path))
        except ParseError:  # its text can hold a local path and the report is pasted back, so nothing is logged
            failed += 1
    elapsed = time.perf_counter() - started
    if failed:
        notes.append(f"parse: {failed} of {len(files)} files did not parse and are not counted")
    return docs, elapsed


def _chunk_texts(docs: list[ParsedDocument], chunker: Chunker) -> list[str]:
    """The text of every chunk of every body and attachment, as the indexer would cut them."""
    texts: list[str] = []
    for doc in docs:
        msg = doc.message
        sender = msg.sender_name or msg.sender_addr
        body_context = context_line_for(sender, msg.sent_ts, msg.subject, "body")
        texts.extend(c.text for c in chunker.chunk(msg.body_text, body_context))
        for att in doc.attachments:
            att_context = context_line_for(sender, msg.sent_ts, msg.subject, f"att:{att.ordinal}")
            texts.extend(c.text for c in chunker.chunk(att.text, att_context))
    return texts


def _embedding_chunker(embedder: OnnxEmbedder | None) -> Chunker:
    """A chunker that counts word pieces when the models package is installed, else whitespace words."""
    if embedder is None:
        return Chunker(None)
    import emailsupersearch_models_en as models

    return Chunker(tokenizer_path(models.embedding_dir()))


def _ocr_pages_per_s(notes: list[str]) -> float | None:
    """Pages per second through the English OCR engine over `OCR_BENCH_PAGES` rendered text images.

    None, with a note, when no engine can run or the engine fails; the note never holds recognised text.
    """
    try:
        from emailsupersearch.ingest.ocr.base import build_engine
    except ImportError:
        notes.append(NO_OCR_NOTE)
        return None
    engine = build_engine("en")  # bench is a process of its own: it may construct an engine, the host never does
    if engine is None:
        notes.append(NO_OCR_ENGINE_NOTE)
        return None
    pages = [
        write_text_png(f"bench page {i} the quarterly invoice for the office lease is attached for review")
        for i in range(OCR_BENCH_PAGES)
    ]
    try:
        started = time.perf_counter()
        for png in pages:
            engine.recognize(png)
        elapsed = time.perf_counter() - started
    except Exception as e:  # noqa: BLE001 - the bench reports a failing engine instead of failing
        notes.append(f"ocr: the {engine.name} engine failed ({type(e).__name__})")
        return None
    return len(pages) / elapsed


def _random_unit_vectors(rng: np.random.Generator, count: int, dim: int) -> np.ndarray:
    vectors = rng.standard_normal((count, dim), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors


def _time_knn(real: np.ndarray, vectors: int, dim: int) -> tuple[float, float, int]:
    """``(p50 ms, p95 ms, rows)`` of `KNN_QUERIES` searches over `real` padded with random vectors to `vectors` rows."""
    rng = np.random.default_rng(0)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = connect(Path(tmp) / "bench.sqlite")
        try:
            migrate(conn)
            index = VectorIndex(conn, dim)
            real = real[:vectors]
            index.upsert([(i + 1, row) for i, row in enumerate(real)])
            rows = len(real)
            while rows < vectors:  # setup, not timed
                count = min(PAD_BATCH, vectors - rows)
                batch = _random_unit_vectors(rng, count, dim)
                index.upsert([(rows + i + 1, row) for i, row in enumerate(batch)])
                rows += count
            durations_ms: list[float] = []
            for query in _random_unit_vectors(rng, KNN_QUERIES, dim):
                started = time.perf_counter()
                index.knn(query, KNN_K, None)
                durations_ms.append((time.perf_counter() - started) * 1000)
            p50, p95 = np.percentile(durations_ms, [50, 95])
            return float(p50), float(p95), index.count()
        finally:
            conn.close()


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _measure(corpus_dir: Path, vectors: int, notes: list[str]) -> BenchReport:
    files = _mail_files(corpus_dir)
    if not files:
        notes.append("corpus: no .eml or .msg files found")
    docs, parse_s = _parse_all(files, notes)
    embedder = load_default_embedder()
    texts = _chunk_texts(docs, _embedding_chunker(embedder))
    embed_rate = p50 = p95 = None
    knn_rows = 0
    if embedder is None:
        notes.append(NO_MODELS_NOTE)
    elif texts:
        started = time.perf_counter()
        embeddings = embedder.embed(texts, batch_size=EMBED_BATCH)
        embed_rate = len(texts) / (time.perf_counter() - started)
        p50, p95, knn_rows = _time_knn(embeddings, vectors, embedder.dim)
    else:
        notes.append("embedder: no chunks to embed")
    ocr_rate = _ocr_pages_per_s(notes)
    return BenchReport(
        parse_files_per_s=round(len(docs) / parse_s, 3) if docs else 0.0,
        embed_chunks_per_s=_rounded(embed_rate),
        ocr_pages_per_s=_rounded(ocr_rate),
        knn_ms_p50=_rounded(p50),
        knn_ms_p95=_rounded(p95),
        knn_vectors=knn_rows,
        peak_rss_mb=round(_peak_working_set_bytes() / MB, 1),
        n_files=len(docs),
        n_chunks=len(texts),
        notes=notes,
    )


def run_bench(corpus_dir: Path | None, n_threads: int = 50, seed: int = 1, vectors: int = 300_000) -> BenchReport:
    """Measure parse, embed and nearest-neighbour speed.

    With no ``corpus_dir`` a synthetic corpus of ``n_threads`` threads is generated into a temporary folder;
    otherwise every ``.eml`` and ``.msg`` under ``corpus_dir`` is used. Without the models package the
    embedding and nearest-neighbour fields are None and ``notes`` says why.
    """
    if vectors < 1:
        raise ValueError(f"vectors must be at least 1, got {vectors}")
    notes: list[str] = []
    if corpus_dir is not None:
        return _measure(Path(corpus_dir), vectors, notes)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        generate(Path(tmp), n_threads, seed)
        return _measure(Path(tmp), vectors, notes)
