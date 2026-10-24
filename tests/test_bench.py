import json
import logging

import pytest

from emailsupersearch.cli import main
from emailsupersearch.diagnostics.bench import (
    NO_MODELS_NOTE,
    NO_OCR_ENGINE_NOTE,
    run_bench,
)


def test_run_bench_without_models_has_notes(monkeypatch):
    monkeypatch.setattr("emailsupersearch.diagnostics.bench.load_default_embedder", lambda threads=4: None)
    r = run_bench(None, n_threads=3, vectors=1000)
    assert r.parse_files_per_s > 0 and NO_MODELS_NOTE in r.notes
    assert r.embed_chunks_per_s is None and r.knn_ms_p50 is None and r.knn_ms_p95 is None and r.knn_vectors == 0


def test_run_bench_with_models(embedder): r = run_bench(None, n_threads=3, vectors=5000); assert r.embed_chunks_per_s > 0 and r.knn_ms_p95 >= r.knn_ms_p50 > 0 and r.knn_vectors == 5000


def test_cli_bench_json(capsys, embedder): assert main(["bench", "--json", "--vectors", "1000"]) == 0 and json.loads(capsys.readouterr().out)["n_files"] > 0


def test_cli_bench_rejects_a_non_positive_vector_count(capsys):
    assert main(["bench", "--vectors", "0"]) == 2  # main returns the exit code argparse gave SystemExit
    assert "not a positive number" in capsys.readouterr().err


def test_run_bench_rejects_a_non_positive_vector_count():
    with pytest.raises(ValueError):
        run_bench(None, n_threads=1, vectors=0)


def test_run_bench_counts_unparsable_files_without_logging_their_names(tmp_path, caplog):
    (tmp_path / "secret-name.msg").write_bytes(b"not a message")
    with caplog.at_level(logging.DEBUG):
        r = run_bench(tmp_path, vectors=10)
    assert r.n_files == 0 and "parse: 1 of 1 files did not parse and are not counted" in r.notes
    assert "secret-name" not in caplog.text and str(tmp_path) not in caplog.text


def test_bench_notes_when_no_engine(monkeypatch):
    monkeypatch.setattr("emailsupersearch.diagnostics.bench.load_default_embedder", lambda threads=4: None)
    monkeypatch.setattr("emailsupersearch.ingest.ocr.base.select_engine", lambda language: None)
    r = run_bench(None, n_threads=1, vectors=10)
    assert r.ocr_pages_per_s is None and NO_OCR_ENGINE_NOTE in r.notes
