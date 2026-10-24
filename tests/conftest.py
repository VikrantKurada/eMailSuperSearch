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
def tokenizer_file() -> Path:
    """The MiniLM ``tokenizer.json``; skipped when the model files have not been fetched."""
    from emailsupersearch.index.embedder import tokenizer_path

    return tokenizer_path(_model_dir_or_skip())


@pytest.fixture(scope="session")
def chunker(tokenizer_file):
    """A chunker that counts the real MiniLM word pieces; skipped when the model files have not been fetched."""
    from emailsupersearch.ingest.chunker import Chunker

    return Chunker(tokenizer_file)


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


@pytest.fixture
def eml_doc(tmp_path):
    """The parsed last message of a one-thread corpus: it carries the PDF attachment."""
    from emailsupersearch.diagnostics.corpus import generate
    from emailsupersearch.ingest.workers import parse_document

    return parse_document(generate(tmp_path / "c", 1, seed=1)[-1])


@pytest.fixture
def eml_doc_with_image(tmp_path):
    """The parsed message of a four-thread corpus whose attachment is an image (thread 3's last file)."""
    from emailsupersearch.diagnostics.corpus import generate
    from emailsupersearch.ingest.workers import parse_document

    for path in reversed(generate(tmp_path / "c4", 4, seed=3)):
        doc = parse_document(path)
        if any(a.kind == "image" for a in doc.attachments):
            return doc
    raise AssertionError("the corpus has no message with an image attachment")


@pytest.fixture
def tmp_store(tmp_path, corpus_dir):
    """A fresh ProjectStore under ``tmp_path / "proj"`` that watches the synthetic corpus folder."""
    from emailsupersearch.store import ProjectStore

    store = ProjectStore.open(tmp_path / "proj")
    store.add_folder(str(corpus_dir), "local")
    yield store
    store.close()


@pytest.fixture
def store_with_chunks(tmp_store):
    """``tmp_store`` holding one message's five chunks: the first three mention "renewal", the fourth an invoice number."""
    from types import SimpleNamespace

    from emailsupersearch.ingest.chunker import ChunkText

    texts = [
        "Please confirm the renewal date before the end of the month.",
        "The annual renewal terms were agreed by both parties last week.",
        "Pricing for the renewal is unchanged from the previous contract.",
        "Invoice INV-2024-00017 is attached",
        "Lunch on Friday works for everyone on the team.",
    ]
    chunks = [ChunkText(idx, text, len(text.split())) for idx, text in enumerate(texts)]
    rowids = tmp_store.replace_chunks("m-fts", "body", chunks)
    return SimpleNamespace(store=tmp_store, conn=tmp_store.conn, rowids=rowids, invoice_rowid=rowids[3])
