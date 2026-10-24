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
