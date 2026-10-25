import sys

import numpy as np
import pytest

from emailsupersearch.index.embedder import load_default_embedder


def test_embedder_shape_and_normalisation(embedder):
    v = embedder.embed(["budget approval for the Acme renewal", "lunch menu"])
    assert v.shape == (2, 384) and v.dtype == np.float32 and np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)


def test_embedder_is_deterministic_and_semantic(embedder):
    a = embedder.embed(["contract renewal terms agreed"])[0]; b = embedder.embed(["we agreed the renewal contract terms"])[0]
    c = embedder.embed(["the printer on floor 3 is jammed"])[0]
    assert float(a @ b) > float(a @ c) and np.allclose(a, embedder.embed(["contract renewal terms agreed"])[0])


def test_embedder_truncates_long_input(embedder): assert embedder.embed(["word " * 2000]).shape == (1, 384)
def test_load_default_embedder_reports_id(embedder): assert load_default_embedder().id == "minilm-l6-v2-int8"


def test_embed_empty_list_returns_empty_matrix(embedder):
    v = embedder.embed([])
    assert v.shape == (0, 384) and v.dtype == np.float32


def test_load_default_embedder_returns_none_when_files_missing(monkeypatch, tmp_path):
    models = pytest.importorskip("emailsupersearch_models_en")
    monkeypatch.setattr(models, "embedding_dir", lambda: tmp_path)
    assert load_default_embedder() is None


def test_load_default_embedder_returns_none_when_models_package_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "emailsupersearch_models_en", None)
    assert load_default_embedder() is None


@pytest.mark.parametrize("error", [ImportError("DLL load failed: blocked by policy at C:/secret"), OSError(5, "denied")])
def test_load_default_embedder_degrades_when_onnxruntime_is_blocked(embedder, monkeypatch, caplog, error):
    import builtins
    import logging

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "onnxruntime":
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with caplog.at_level(logging.WARNING, logger="emailsupersearch"):
        assert load_default_embedder() is None
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(type(error).__name__ in m and "keyword-only" in m for m in messages)
    assert not any("secret" in m or "DLL" in m for m in messages)  # the type only, never the text


def test_the_host_starts_keyword_only_when_onnxruntime_is_blocked():
    import subprocess

    script = (
        "import importlib.abc, sys\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'onnxruntime':\n"
        "            raise ImportError('DLL load failed: blocked by policy')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import emailsupersearch.host.app, emailsupersearch.host.launcher\n"
        "from emailsupersearch.index.embedder import load_default_embedder\n"
        "from emailsupersearch.ingest.chunker import Chunker\n"
        "assert load_default_embedder() is None\n"
        "print(len(Chunker(None).chunk('some words to index', 'From x')))\n"
    )
    done = subprocess.run([sys.executable, "-c", script], check=False, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "1"
