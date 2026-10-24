import pytest


@pytest.fixture(scope="session")
def embedder():
    """The real int8 MiniLM embedder; skipped when the model files have not been fetched."""
    try:
        import emailsupersearch_models_en as models
    except ImportError:
        pytest.skip("run scripts/fetch_models.py")
    from emailsupersearch.index.embedder import OnnxEmbedder, tokenizer_path

    model_dir = models.embedding_dir()
    if not (model_dir / "model.onnx").is_file() or not tokenizer_path(model_dir).is_file():
        pytest.skip("run scripts/fetch_models.py")
    return OnnxEmbedder(model_dir)
