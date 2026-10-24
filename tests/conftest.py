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
def conn(tmp_path):
    """A migrated, empty project database at ``tmp_path / "q.sqlite"``; closed at teardown."""
    from emailsupersearch.db.connection import connect
    from emailsupersearch.db.schema import migrate

    c = connect(tmp_path / "q.sqlite")
    migrate(c)
    yield c
    c.close()


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


@pytest.fixture(scope="session")
def parse_pool():
    """One parse worker pool for the whole session; its workers are killed at teardown."""
    from emailsupersearch.ingest.workers import ParsePool

    pool = ParsePool()
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def embedder_or_none():
    """The real embedder when the models package is installed, else None (the keyword-only install)."""
    from emailsupersearch.index.embedder import load_default_embedder

    return load_default_embedder()


def _make_pipeline(store, cache_dir, chunker, embedder, pool, enqueue_ocr):
    from emailsupersearch.index.vectors import VectorIndex
    from emailsupersearch.ingest.parse_cache import ParseCache
    from emailsupersearch.ingest.pipeline import IngestPipeline

    vectors = VectorIndex(store.conn) if embedder is not None else None
    return IngestPipeline(store, ParseCache(cache_dir), pool, chunker, embedder, vectors, enqueue_ocr=enqueue_ocr)


@pytest.fixture(scope="session")
def indexed_store(tmp_path_factory, chunker, embedder_or_none, parse_pool):
    """A ProjectStore holding the fully ingested 12-thread corpus (seed 5), shared by the whole session.

    It embeds when the models are installed. Tests that change it restore what they changed; it is closed at
    session end.
    """
    from emailsupersearch.diagnostics.corpus import generate
    from emailsupersearch.ingest.scan import reconcile
    from emailsupersearch.store import ProjectStore

    root = tmp_path_factory.mktemp("indexed")
    corpus = root / "corpus"
    generate(corpus, 12, seed=5)
    store = ProjectStore.open(root / "proj")
    store.add_folder(str(corpus), "local")
    pipeline = _make_pipeline(store, root / "cache", chunker, embedder_or_none, parse_pool, None)
    pipeline.apply_scan(reconcile(store, [corpus], 1, now=1))
    yield store
    store.close()


@pytest.fixture
def pipeline(tmp_store, tmp_path, chunker, embedder_or_none, parse_pool):
    """An ingest pipeline over ``tmp_store`` with no OCR engine; it embeds when the models are installed."""
    return _make_pipeline(tmp_store, tmp_path / "cache", chunker, embedder_or_none, parse_pool, None)


@pytest.fixture
def pipeline_with_queue(tmp_store, tmp_path, chunker, embedder_or_none, parse_pool):
    """``pipeline`` with an OCR queue: every job it enqueues is appended to ``pipeline_with_queue.jobs``."""
    jobs: list[dict] = []
    pipeline = _make_pipeline(
        tmp_store, tmp_path / "cache", chunker, embedder_or_none, parse_pool,
        lambda payload: (jobs.append(payload), len(jobs))[1],
    )
    pipeline.jobs = jobs
    return pipeline


@pytest.fixture
def make_supervisor(embedder_or_none, chunker):
    """Build a supervisor over ``tmp_store`` through ``build_project_supervisor`` with the given reconcile interval
    and OCR on or off; the events it emits are collected in ``sup.events_for_tests``.

    The supervisor owns its parse pool and closes it, with the store, when it is stopped.
    """
    from emailsupersearch.jobs.supervisor import build_project_supervisor

    def make(tmp_store, corpus_dir, interval_s, with_ocr):
        tmp_store.set_setting("reconcile_interval_s", interval_s)
        tmp_store.set_setting("ocr_enabled", with_ocr)
        events: list[dict] = []
        sup = build_project_supervisor(
            "p", tmp_store, embedder_or_none, chunker, corpus_dir.parent / "cache", events.append
        )
        sup.events_for_tests = events
        return sup

    return make


@pytest.fixture
def sup(make_supervisor, tmp_store, corpus_dir):
    """A supervisor with the default 900 s reconcile interval and no OCR; stopped at teardown."""
    s = make_supervisor(tmp_store, corpus_dir, 900, False)
    yield s
    s.stop()


@pytest.fixture
def sup_fast(make_supervisor, tmp_store, corpus_dir):
    """A supervisor that reconciles every second; no OCR."""
    s = make_supervisor(tmp_store, corpus_dir, 1, False)
    yield s
    s.stop()


@pytest.fixture
def sup_slow(make_supervisor, tmp_store, corpus_dir):
    """A supervisor whose timer never fires during a test (3600 s), so only the watcher can trigger a rescan."""
    s = make_supervisor(tmp_store, corpus_dir, 3600, False)
    yield s
    s.stop()


@pytest.fixture
def sup_with_ocr(make_supervisor, tmp_store, corpus_dir):
    """A supervisor with OCR wired; skipped when no OCR engine can run here."""
    from emailsupersearch.ingest.ocr.base import select_engine

    if select_engine("en") is None:
        pytest.skip("no OCR engine: install the Windows OCR language pack or emailsupersearch[ocr]")
    s = make_supervisor(tmp_store, corpus_dir, 900, True)
    yield s
    s.stop()


@pytest.fixture
def host_services(tmp_path, chunker_ws):
    """``Services`` over a fresh catalog in ``tmp_path``, keyword-only (no embedder); supervisors started through the
    API are stopped, and the catalog closed, at teardown."""
    from emailsupersearch.catalog import Catalog
    from emailsupersearch.host.app import Services
    from emailsupersearch.host.security import SessionStore, new_token
    from emailsupersearch.host.sse import EventBus
    from emailsupersearch.jobs.supervisor import build_project_supervisor

    catalog = Catalog(tmp_path)
    bus = EventBus()
    services = Services(
        catalog=catalog,
        embedder=None,
        chunker=chunker_ws,
        supervisors={},
        bus=bus,
        launch_token=new_token(),
        sessions=SessionStore(),
        start_supervisor=lambda pid: build_project_supervisor(
            pid,
            catalog.open_store(pid),
            None,
            chunker_ws,
            tmp_path / "cache" / "parsed",
            lambda ev: bus.publish(f"sync:{pid}", ev),
        ),
        static_dir=None,
        request_shutdown=lambda: None,
        blocked_projects={},
    )
    yield services
    for sup in list(services.supervisors.values()):
        sup.stop()
    catalog.close()


@pytest.fixture
def token(host_services):
    """The host's launch token."""
    return host_services.launch_token


@pytest.fixture
def raw_client(host_services):
    """A TestClient on the host app with a loopback Host header and no cookies; the app's lifespan runs."""
    from starlette.testclient import TestClient

    from emailsupersearch.host.app import create_app

    with TestClient(create_app(host_services), base_url="http://127.0.0.1") as c:
        yield c


@pytest.fixture
def client_no_header(raw_client, token):
    """``raw_client`` holding the session and CSRF cookies from ``/auth``, without the CSRF header."""
    r = raw_client.get(f"/auth?token={token}", follow_redirects=False)
    assert r.status_code == 303
    return raw_client


@pytest.fixture
def client(client_no_header):
    """``client_no_header`` that also sends the CSRF header on every request."""
    client_no_header.headers["X-ESS-Token"] = client_no_header.cookies["ess_csrf"]
    return client_no_header


@pytest.fixture
def project(client, corpus_dir):
    """The id of a project over ``corpus_dir`` created through the API (its supervisor is running)."""
    r = client.post("/api/projects", json={"name": "Corpus", "folders": [str(corpus_dir)], "language": "en"})
    assert r.status_code == 201
    return r.json()["id"]
