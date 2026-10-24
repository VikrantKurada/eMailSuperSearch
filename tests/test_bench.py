import json

from emailsupersearch.cli import main
from emailsupersearch.diagnostics.bench import run_bench


def test_run_bench_without_models_has_notes(monkeypatch):
    monkeypatch.setattr("emailsupersearch.diagnostics.bench.load_default_embedder", lambda threads=4: None)
    r = run_bench(None, n_threads=3, vectors=1000); assert r.parse_files_per_s > 0 and r.embed_chunks_per_s is None and r.notes
def test_run_bench_with_models(embedder): r = run_bench(None, n_threads=3, vectors=5000); assert r.embed_chunks_per_s > 0 and r.knn_ms_p95 >= r.knn_ms_p50 > 0 and r.knn_vectors == 5000
def test_cli_bench_json(capsys, embedder): assert main(["bench", "--json", "--vectors", "1000"]) == 0 and json.loads(capsys.readouterr().out)["n_files"] > 0
