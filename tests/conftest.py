from pathlib import Path

import pytest


def _model_dir_or_skip() -> Path:
    """The fetched embedding model directory, or skip the test when its files are missing."""
    try:
        import emailsupersearch_models_en as models
    except ImportError:
        pytest.skip("run scripts/fetch_models.py")
    from emailsupersearch.index.embedder import tokenizer_path

    model_dir = models.embedding_dir()
    if not (model_dir / "model.onnx").is_file() or not tokenizer_path(model_dir).is_file():
        pytest.skip("run scripts/fetch_models.py")
    return model_dir


@pytest.fixture(scope="session")
def embedder():
    """The real int8 MiniLM embedder; skipped when the model files have not been fetched."""
    from emailsupersearch.index.embedder import OnnxEmbedder

    return OnnxEmbedder(_model_dir_or_skip())


@pytest.fixture(scope="session")
def chunker():
    """A chunker that counts the real MiniLM word pieces; skipped when the model files have not been fetched."""
    from emailsupersearch.index.embedder import tokenizer_path
    from emailsupersearch.ingest.chunker import Chunker

    return Chunker(tokenizer_path(_model_dir_or_skip()))


@pytest.fixture(scope="session")
def chunker_ws():
    """The keyword-only install's chunker: a token is a whitespace-separated word."""
    from emailsupersearch.ingest.chunker import Chunker

    return Chunker(None)


@pytest.fixture
def parsed_factory():
    """Build a ParsedMessage with sensible defaults; keyword overrides replace any field."""
    from emailsupersearch.types import ParsedMessage, Recipient

    def make(**overrides):
        fields = {
            "message_id": "<default@example.com>",
            "subject": "Hello",
            "sender_name": "Jane Doe",
            "sender_addr": "jane@example.com",
            "recipients": [Recipient("to", "Bob", "bob@example.org")],
            "sent_ts": 1773705600,
            "received_ts": 1773705660,
            "date_source": "header_date",
            "body_text": "hello",
            "body_html": None,
            "in_reply_to": None,
            "references": [],
            "conversation_index": None,
            "conversation_topic": None,
            "attachments": [],
        }
        fields.update(overrides)
        return ParsedMessage(**fields)

    return make


@pytest.fixture
def corpus_dir(tmp_path):
    """A directory holding the deterministic six-thread synthetic corpus."""
    from emailsupersearch.diagnostics.corpus import generate

    generate(tmp_path / "corpus", 6, seed=1)
    return tmp_path / "corpus"


@pytest.fixture(scope="session")
def msg_fixtures():
    """Sorted ``.msg`` files from extract-msg's samples and the owner's own; skipped when neither exists."""
    root = Path(__file__).resolve().parent / "fixtures"
    found = sorted(p for sub in ("msg", "owner") for p in (root / sub).glob("*.msg"))
    if not found:
        pytest.skip("run scripts/fetch_fixtures.py")
    return found
