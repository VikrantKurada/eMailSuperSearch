import numpy as np

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
