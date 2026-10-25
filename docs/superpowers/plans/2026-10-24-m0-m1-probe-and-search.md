# eMailSuperSearch M0 + M1 (Probe and Search) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `emailsupersearch` core as a pip-installable package that a standard Windows user can run with `python -m emailsupersearch` to create a project over local `.msg`/`.eml` folders, keep it mirrored in the background, search it by meaning and keyword with sender, recipient, date and attachment filters, read messages in the browser, and export results to Excel; plus the `doctor` and `bench` probe commands that get run on the office laptop first.

**Architecture:** One host process (Starlette on uvicorn, 127.0.0.1, token-gated) owns a per-project SQLite file (manifest, messages, FTS5, sqlite-vec vectors, jobs), a reconcile-scan sync loop with a watchfiles trigger, parse workers in subprocesses, and an isolated OCR worker. A Vite+Preact single-page app compiled at build time ships inside the wheel. Embeddings come from an int8 ONNX MiniLM in a separate `emailsupersearch-models-en` wheel. Chat, the LLM engine, the knowledge graph and artefacts are later milestones and are not in this plan; their tabs appear as placeholders.

**Tech Stack:** Python 3.12/3.13, hatchling, extract-msg, pypdfium2, python-docx, openpyxl, python-pptx, Pillow, onnxruntime, tokenizers, numpy, sqlite-vec, watchfiles, Starlette, uvicorn, sse-starlette, winrt-Windows.Media.Ocr (+ companions), rapidocr (extra), pytest, httpx, pypdf (dev); Vite, Preact, TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-10-24-emailsupersearch-design.md` (sections 6 to 10, 14 to 18 and 20 apply to this plan). Mockups: `docs/superpowers/specs/mockups/2026-10-24-layout.html`.

## Global Constraints

- Requires-Python `>=3.12,<3.14`. On the development machine the default `python` is 3.14, so every `python` in this plan means the project venv interpreter `.venv/Scripts/python`, created once with `py -3.12 -m venv .venv` (Git Bash path syntax throughout).
- The install line, everywhere it appears (plan, README, `docs/install.md`, CI, spec 7.3): `pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console ...`. Those two are pure-Python dependencies of extract-msg that exist only as source archives and build without a compiler; every other dependency must resolve to a `win_amd64` or pure wheel.
- Licence `GPL-3.0-or-later` for `emailsupersearch`; `emailsupersearch-models-en` passes through Apache-2.0 (MiniLM) now, and MIT is added with the spaCy model in M3.
- All state under `%LOCALAPPDATA%\eMailSuperSearch\` (override with `EMAILSUPERSEARCH_DATA_ROOT`), laid out as in spec 6.2; data roots containing an `OneDrive` path segment, or under a user's `Documents` or `Desktop`, are refused by `validate_data_root` and by `serve`/`sync` at start.
- Zero network: `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_DISABLE_UPDATE_CHECK=1`, `ORT_DISABLE_TELEMETRY=1` set at package import before any dependency loads; `onnxruntime.disable_telemetry_events()` called before the first session; rapidocr constructed with explicit paths to its bundled models; no CDN references in the UI.
- Server binds `127.0.0.1` only; default port `8765`, fallback to a free port; uvicorn `access_log=False`. The launch token (owner-only file `host.token`) is accepted only at `/auth?token=` and, as header `X-ESS-Launch`, at `/api/shutdown`. `/auth` mints a browser session: cookie `ess_session` (`HttpOnly; SameSite=Strict`) plus a readable cookie `ess_csrf`; every `/api/*` request needs a valid `ess_session`; every non-GET also needs header `X-ESS-Token` equal to that session's `ess_csrf`. Host must be `localhost`, `127.0.0.1` or `[::1]` (any port); a present `Origin` must be a loopback origin.
- Pins: `extract-msg>=0.56,<0.57`; chunks of about 220 tokens with 15% overlap; every chunk is keyword-indexed, only the first 64 chunks of an attachment are embedded (`partial_embed` flag beyond that); attachments over 25 MB are not parsed (`unsupported`, reason `too_large`); OCR at most 50 pages and 10 minutes per attachment; `cid:` inline images under 100 KB skipped; reconcile every 900 s and at host start; tombstones purged only on the following scan and hidden from search meanwhile; RRF `k=60`; 50 candidates per retriever; page size 20; Excel export cap 5,000 rows; parse worker timeout 120 s and memory cap 1,024 MB; logs `RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`.
- Message identity is `message_key` (spec 8.3), never the path. Both `.msg` and `.eml` are ingested.
- Dates shown or formatted anywhere use UTC.
- Commit after every task with the message given; the repository is initialised in Task 1 and all work is on `main` until the owner asks otherwise.

## Review Focus

1. A `.msg` whose body exists only as compressed RTF, or is in a non-Unicode code page, must still yield `body_text` (Task 8 test `test_parse_msg_rtf_only_body_has_text`; it runs over the fetched fixtures and the owner's samples in `tests/fixtures/owner`, and is the only test of this input until those samples exist).
2. Two files that are the same email exported twice (same Message-ID, different bytes) must become one message with two paths, and deleting one path must keep the message (Task 19 test `test_duplicate_export_indexes_once_and_survives_one_delete`).
3. A watched root that is temporarily missing (drive unplugged, share offline) must abort the scan without tombstoning anything (Task 18 test `test_missing_root_aborts_without_tombstones`).
4. A query containing FTS5 syntax characters (`"`, `*`, `:`, `-`, `(`) or nothing but whitespace must return results or nothing, never an SQL error (Task 16 test `test_sanitize_query_never_raises`).
5. An attachment over 25 MB or a password-protected PDF must leave the message indexed with the attachment flagged `unsupported`, never fail the file (Task 9 tests `test_oversized_attachment_flagged_unsupported_too_large`, `test_encrypted_pdf_flagged_unsupported`).

---

## File structure

```
pyproject.toml                                  core distribution (hatchling)
LICENSE  README.md  .gitignore  requirements-lock-3.12.txt  requirements-lock-3.13.txt
.github/workflows/ci.yml
packages/models-en/pyproject.toml               emailsupersearch-models-en distribution
packages/models-en/src/emailsupersearch_models_en/__init__.py   embedding_dir(), EMBEDDER_ID, EMBEDDING_DIM
packages/models-en/src/emailsupersearch_models_en/data/         model.onnx, tokenizer.json (fetched, gitignored)
scripts/fetch_models.py                         downloads the MiniLM files into the models package (dev/CI only)
scripts/fetch_fixtures.py                       downloads extract-msg's example .msg files into tests/fixtures/msg (dev only)
ui/                                             Vite + Preact + TypeScript source; builds into src/emailsupersearch/ui/dist
src/emailsupersearch/
  __init__.py            __version__; calls env.apply_zero_network_env()
  __main__.py            cli.main()
  cli.py                 argparse: serve (default) | stop | doctor | bench | sync | purge
  env.py                 zero-network environment variables
  paths.py               data root, project dirs, validation
  logging_setup.py       rotating handlers, redaction filter, log_subject
  types.py               dataclasses shared by every layer
  catalog.py             catalog.sqlite: projects list, global settings, doctor runs
  store.py               ProjectStore: single-writer facade over project.sqlite
  db/connection.py       connect() with WAL, sqlite-vec, FTS5 check
  db/schema.py           MIGRATIONS, migrate(), SchemaTooNew
  diagnostics/corpus.py  deterministic synthetic .eml corpus (shipped: bench uses it)
  diagnostics/doctor.py  run_doctor, render_report
  diagnostics/bench.py   run_bench
  ingest/identity.py     normalize_addr, normalize_subject, message_key
  ingest/eml_adapter.py  parse_eml
  ingest/msg_adapter.py  parse_msg (extract-msg)
  ingest/parse.py        parse_file dispatch, ParseError
  ingest/attachments.py  extract_text matrix, caps, statuses
  ingest/hygiene.py      strip_quoted, strip_signature, new_text
  ingest/chunker.py      Chunker
  ingest/parse_cache.py  ParseCache (shared, by sha256)
  ingest/workers.py      ParsePool (subprocess, timeout, memory cap)
  ingest/scan.py         reconcile()
  ingest/pipeline.py     IngestPipeline
  ingest/watcher.py      FolderWatcher
  ingest/ocr/base.py     OcrEngine protocol, select_engine
  ingest/ocr/windows_ocr.py, rapid_ocr.py, worker.py
  index/embedder.py      OnnxEmbedder, load_default_embedder
  index/fts.py           FTS5 queries
  index/vectors.py       VectorIndex (sqlite-vec)
  index/chains.py        conversation-index parsing, assign_chain, update_chain_row
  search/filters.py      SearchFilters -> candidate keys
  search/hybrid.py       search(), rrf()
  search/results.py      card dataclasses
  export/excel.py        export_results
  jobs/queue.py          durable job queue
  jobs/supervisor.py     ProjectSupervisor, build_project_supervisor
  host/security.py       sessions, middleware
  host/app.py            create_app(services), Services
  host/routes_projects.py, routes_search.py, routes_messages.py, routes_export.py, routes_sync.py, routes_settings.py
  host/sse.py            EventBus
  host/lock.py           InstanceLock, running info
  host/launcher.py       serve(), stop()
  host/sanitize.py       sanitize_html
  host/folder_picker.py  pick_folder
  ui/dist/               compiled SPA (package data, gitignored, built in CI)
tests/                   pytest; conftest.py fixtures; helpers.py; fixtures/msg fetched; fixtures/owner gitignored
```

Fixtures and helpers introduced by task (all in `tests/conftest.py` unless noted): Task 3 `embedder`; Task 6 `parsed_factory`; Task 7 `corpus_dir`, `tests/helpers.py` (`make_eml_bytes`, `wait_until`, attachment builders, `LONG_TEXT_3000_WORDS`, `LONG_TEXT_30000_WORDS`); Task 8 `msg_fixtures`; Task 11 `chunker`; Task 12 `eml_doc`, `eml_doc_with_image`; Task 15 `tmp_store`; Task 16 `store_with_chunks`; Task 19 `pipeline`, `pipeline_with_queue`; Task 20 `conn`; Task 21 `sup`, `sup_fast`, `sup_slow`, `sup_with_ocr`; Task 23 `indexed_store`; Task 26 `host_services`, `token`, `raw_client`, `client_no_header`, `client`, `project`; Task 27 `indexed_project`.

---

### Task 1: Repository, venv, package skeleton, paths and zero-network environment

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `README.md`, `.gitignore`, `src/emailsupersearch/__init__.py`, `src/emailsupersearch/__main__.py`, `src/emailsupersearch/env.py`, `src/emailsupersearch/paths.py`, `tests/conftest.py`
- Test: `tests/test_paths.py`, `tests/test_env.py`

**Interfaces:**
- Produces: `paths.data_root() -> Path`; `paths.validate_data_root(p: Path) -> str | None` (reason string when refused, else `None`): refused when any path part equals `OneDrive` (case-insensitive), or when the path matches `<drive>:\Users\<name>\(Documents|Desktop)\...` (case-insensitive), or when it is under the real `%USERPROFILE%\Documents` or `\Desktop`; `paths.project_dir(root: Path, project_id: str) -> Path` (= `root / "projects" / project_id`); `paths.ensure_layout(root: Path) -> None` (creates `projects`, `cache/parsed`, `models`, `logs`); `env.apply_zero_network_env() -> None`; `env.ZERO_NETWORK_VARS: dict[str, str]`.

- [ ] **Step 1: Initialise the repository, the venv, and commit the existing documents**

```bash
cd /d/Projects/eMailSuperSearch && git init -b main
printf '.venv/\n__pycache__/\n*.pyc\ndist/\nbuild/\n*.egg-info/\n.superpowers/\nsrc/emailsupersearch/ui/dist/\nui/node_modules/\npackages/models-en/src/emailsupersearch_models_en/data/\ntests/fixtures/msg/\ntests/fixtures/owner/\n.pytest_cache/\n' > .gitignore
git add .gitignore docs eMailSuperSearch_PRD.txt && git commit -m "docs: PRD, research, design spec, mockups and plan"
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -U pip pytest
```

- [ ] **Step 2: Write the failing tests**

`tests/test_paths.py`:
```python
def test_data_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", str(tmp_path))
    assert data_root() == tmp_path

def test_data_root_default_is_localappdata(monkeypatch):
    monkeypatch.delenv("EMAILSUPERSEARCH_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert data_root() == Path(r"C:\Users\x\AppData\Local\eMailSuperSearch")

@pytest.mark.parametrize("bad", [r"C:\Users\x\OneDrive\ess", r"D:\onedrive\ess", r"C:\Users\x\Documents\ess", r"C:\Users\x\Desktop\ess"])
def test_validate_data_root_refuses_synced_and_known_folders(bad):
    assert validate_data_root(Path(bad)) is not None

def test_validate_data_root_accepts_localappdata():
    assert validate_data_root(Path(r"C:\Users\x\AppData\Local\eMailSuperSearch")) is None

def test_ensure_layout_creates_dirs(tmp_path):
    ensure_layout(tmp_path)
    assert all((tmp_path / d).is_dir() for d in ["projects", "cache/parsed", "models", "logs"])
```
`tests/test_env.py`:
```python
def test_zero_network_env_applied(monkeypatch):
    for k in ZERO_NETWORK_VARS: monkeypatch.delenv(k, raising=False)
    apply_zero_network_env()
    assert all(os.environ[k] == v for k, v in ZERO_NETWORK_VARS.items())

def test_zero_network_env_does_not_override_explicit_value(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0"); apply_zero_network_env()
    assert os.environ["HF_HUB_OFFLINE"] == "0"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_paths.py tests/test_env.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emailsupersearch'`

- [ ] **Step 4: Create `pyproject.toml`, `LICENSE`, `README.md`, the package and the two modules**

`pyproject.toml`: hatchling backend; `name = "emailsupersearch"`, `version = "0.0.1"`, `requires-python = ">=3.12,<3.14"`, `license = "GPL-3.0-or-later"`; dependencies: `extract-msg>=0.56,<0.57`, `pypdfium2>=5`, `python-docx>=1.2`, `openpyxl>=3.1`, `python-pptx>=1.0`, `Pillow>=11`, `numpy>=2`, `onnxruntime>=1.31`, `tokenizers>=0.21`, `sqlite-vec>=0.1.6`, `watchfiles>=1.0`, `starlette>=1.0`, `uvicorn>=0.34`, `sse-starlette>=2`, `python-multipart>=0.0.20`, and under marker `sys_platform == 'win32'`: `winrt-runtime>=3`, `winrt-Windows.Media.Ocr>=3`, `winrt-Windows.Globalization>=3`, `winrt-Windows.Graphics.Imaging>=3`, `winrt-Windows.Storage.Streams>=3`, `winrt-Windows.Foundation>=3`, `winrt-Windows.Foundation.Collections>=3`; extras `nlp = ["emailsupersearch-models-en>=0.1"]`, `ocr = ["rapidocr>=3.10,<4", "opencv-python-headless>=4.10"]`, `all = ["emailsupersearch[nlp,ocr]"]`, `dev = ["pytest>=8", "httpx>=0.27", "ruff>=0.6", "pypdf>=5", "build>=1.2"]`; `[tool.hatch.build.targets.wheel] packages = ["src/emailsupersearch"]`, `artifacts = ["src/emailsupersearch/ui/dist/**"]`; `[tool.pytest.ini_options] testpaths = ["tests"]`.

`LICENSE`: the verbatim GPL-3.0 text from `https://www.gnu.org/licenses/gpl-3.0.txt`. `README.md`: title, one-paragraph purpose, and the install from spec 7.3 using the Global Constraints install line (the Probe section is added in Task 14).

`env.py`: `ZERO_NETWORK_VARS = {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_UPDATE_CHECK": "1", "ORT_DISABLE_TELEMETRY": "1"}`; `apply_zero_network_env()` uses `os.environ.setdefault`. `__init__.py` defines `__version__ = "0.0.1"` and calls `apply_zero_network_env()`. `__main__.py`: `from emailsupersearch.cli import main; raise SystemExit(main())` inside `if __name__ == "__main__":` (the module arrives in Task 5). `paths.py` as in Interfaces.

- [ ] **Step 5: Install and run the tests**

Run: `.venv/Scripts/python -m pip install -e ".[dev]" --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console && .venv/Scripts/python -m pytest tests/test_paths.py tests/test_env.py -v`
Expected: PASS (10 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml LICENSE README.md src tests && git commit -m "feat: package skeleton, data-root paths and zero-network environment"
```

### Task 2: Logging with redaction

**Files:**
- Create: `src/emailsupersearch/logging_setup.py`
- Test: `tests/test_logging_setup.py`

**Interfaces:**
- Produces: `configure_logging(process_name: str, root: Path, level: str = "INFO") -> logging.Logger` (handler `root/"logs"/f"{process_name}.log"`, `maxBytes=5*1024*1024`, `backupCount=3`; adds the handler only once per name but always applies the requested level to the logger and the handler, attaching or detaching `RedactingFilter` accordingly); `class RedactingFilter(logging.Filter)` applied when level is not DEBUG; `redact(text: str) -> str`; `log_subject(subject: str) -> str` (returns the subject unchanged when the `emailsupersearch` logger's effective level is DEBUG, else `<subj:sha1[:8]>`); `PATH_RE` pinned as `(?:[A-Za-z]:|\\\\[^\\\s]+)(?:\\[^\\/:*?"<>|\r\n]+)+\.(?:msg|eml|pdf|docx|xlsx|xlsm|pptx)\b` (segments may contain spaces), applied before the e-mail regex `[\w.+-]+@[\w-]+(?:\.[\w-]+)+`.

- [ ] **Step 1: Write the failing tests**

```python
def test_redact_hashes_email_addresses_and_paths():
    out = redact(r"sent by jane.doe@example.com from C:\Users\jane\Archive\RE Budget.msg")
    assert "jane.doe@example.com" not in out and "jane" not in out
    assert re.search(r"<addr:[0-9a-f]{8}>", out) and re.search(r"<path:[0-9a-f]{8}>", out)

def test_redact_unc_path(): assert r"\\server" not in redact(r"\\server\share\Q1 Report.pdf")

def test_info_handler_redacts_but_debug_does_not(tmp_path):
    log = configure_logging("t", tmp_path, "INFO"); log.info("mail from a@b.com")
    assert "a@b.com" not in (tmp_path / "logs" / "t.log").read_text()
    log2 = configure_logging("t2", tmp_path, "DEBUG"); log2.debug("mail from a@b.com")
    assert "a@b.com" in (tmp_path / "logs" / "t2.log").read_text()

def test_log_subject_hashes_unless_debug(tmp_path):
    configure_logging("emailsupersearch", tmp_path, "INFO"); assert log_subject("RE: Acme") != "RE: Acme"
    configure_logging("emailsupersearch", tmp_path, "DEBUG"); assert log_subject("RE: Acme") == "RE: Acme"

def test_handler_is_rotating_with_spec_limits(tmp_path):
    h = [h for h in configure_logging("r", tmp_path).handlers if isinstance(h, RotatingFileHandler)][0]
    assert h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 3
```

- [ ] **Step 2: Run to verify failure** — `.venv/Scripts/python -m pytest tests/test_logging_setup.py -v` → FAIL (module missing)
- [ ] **Step 3: Implement** as in Interfaces; `redact` replaces each match by `<path:…>` / `<addr:…>` using `hashlib.sha1(match.lower().encode()).hexdigest()[:8]`; the filter mutates `record.msg` and string `record.args`.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src/emailsupersearch/logging_setup.py tests/test_logging_setup.py && git commit -m "feat: rotating logs with INFO-level redaction"`

### Task 3: Models package and ONNX embedder

**Files:**
- Create: `packages/models-en/pyproject.toml`, `packages/models-en/src/emailsupersearch_models_en/__init__.py`, `scripts/fetch_models.py`, `src/emailsupersearch/index/__init__.py`, `src/emailsupersearch/index/embedder.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_embedder.py`

**Interfaces:**
- Produces: `emailsupersearch_models_en.embedding_dir() -> Path` (contains `model.onnx` and `tokenizer.json`), `EMBEDDER_ID = "minilm-l6-v2-int8"`, `EMBEDDING_DIM = 384`; `class OnnxEmbedder: __init__(model_dir: Path, threads: int = 4)` (`SessionOptions.intra_op_num_threads = threads`); `id: str`; `dim: int`; `max_tokens: int = 256`; `embed(texts: list[str], batch_size: int = 32) -> np.ndarray` (shape `(n, 384)`, float32, L2-normalised; mean pooling over the attention mask); `load_default_embedder(threads: int = 4) -> OnnxEmbedder | None` (None when the models package is absent); `tokenizer_path(model_dir: Path) -> Path`.
- `scripts/fetch_models.py` behaviour: `EXPECTED_SHA256 = {"model.onnx": None, "tokenizer.json": None}` at first; a `None` entry means download, print `name sha256 size` and exit 0 telling the engineer to paste the hash in; a set entry means skip when the file exists with that hash, otherwise download and exit 1 (deleting the file) on mismatch. Sources: `https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_quint8_avx2.onnx` → `model.onnx`; `.../resolve/main/tokenizer.json` → `tokenizer.json`.
- conftest fixture `embedder` (session scope): skips with "run scripts/fetch_models.py" when the files are missing.

- [ ] **Step 1: Write `scripts/fetch_models.py`**, run it twice (second run pins the hashes), commit the hashes.
- [ ] **Step 2: Write the failing tests**

```python
def test_embedder_shape_and_normalisation(embedder):
    v = embedder.embed(["budget approval for the Acme renewal", "lunch menu"])
    assert v.shape == (2, 384) and v.dtype == np.float32 and np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)

def test_embedder_is_deterministic_and_semantic(embedder):
    a = embedder.embed(["contract renewal terms agreed"])[0]; b = embedder.embed(["we agreed the renewal contract terms"])[0]
    c = embedder.embed(["the printer on floor 3 is jammed"])[0]
    assert float(a @ b) > float(a @ c) and np.allclose(a, embedder.embed(["contract renewal terms agreed"])[0])

def test_embedder_truncates_long_input(embedder): assert embedder.embed(["word " * 2000]).shape == (1, 384)
def test_load_default_embedder_reports_id(embedder): assert load_default_embedder().id == "minilm-l6-v2-int8"
```

- [ ] **Step 3: Run to verify failure** → FAIL (import errors)
- [ ] **Step 4: Implement** the models package (`pyproject.toml`: name `emailsupersearch-models-en`, version `0.1.0`, hatchling, `artifacts = ["src/emailsupersearch_models_en/data/**"]`, LICENSE naming Apache-2.0 for MiniLM) and `OnnxEmbedder` (`tokenizers.Tokenizer.from_file`, `enable_truncation(max_length=256)`, `enable_padding()`, `onnxruntime.disable_telemetry_events()` then `InferenceSession(..., providers=["CPUExecutionProvider"])`). Install: `.venv/Scripts/python -m pip install -e packages/models-en`.
- [ ] **Step 5: Run to verify pass** → PASS
- [ ] **Step 6: Commit** — `git add packages scripts/fetch_models.py src/emailsupersearch/index tests && git commit -m "feat: models-en package and int8 MiniLM ONNX embedder"`

### Task 4: Database connection and schema migrations

**Files:**
- Create: `src/emailsupersearch/db/__init__.py`, `src/emailsupersearch/db/connection.py`, `src/emailsupersearch/db/schema.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `class MissingSqliteFeature(Exception)`; `connect(path: Path) -> sqlite3.Connection` (`check_same_thread=False` because the supervisor thread and request handlers share a project's connection, WAL, `busy_timeout=5000`, `foreign_keys=ON`, `row_factory=sqlite3.Row`, sqlite-vec loaded; raises `MissingSqliteFeature("fts5")` when FTS5 is absent); `SCHEMA_VERSION: int`; `MIGRATIONS: list[tuple[int, str]]`; `class SchemaTooNew(Exception)`; `migrate(conn) -> int` (raises `SchemaTooNew` when the stored version exceeds `SCHEMA_VERSION`, else applies every migration above the stored version, one transaction each, and returns the version); `schema_version(conn) -> int`.
- Tables created by migration 1 (columns per spec 8.2): `settings(key TEXT PRIMARY KEY, value TEXT)`, `folders(path TEXT PRIMARY KEY, kind TEXT, last_scan_id INTEGER, reachable INTEGER)`, `files(path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT, file_id TEXT, message_key TEXT, parse_status TEXT, status_reason TEXT, last_seen_scan_id INTEGER, tombstoned_at INTEGER)`, `messages(message_key TEXT PRIMARY KEY, message_id TEXT, sent_ts INTEGER, received_ts INTEGER, date_source TEXT, subject TEXT, normalized_subject TEXT, sender_name TEXT, sender_addr TEXT, chain_id TEXT, chain_depth INTEGER, body_text TEXT, body_html TEXT, new_text TEXT, has_attachments INTEGER, flags TEXT)`, `recipients(message_key, kind, name, addr)`, `attachments(message_key, ordinal, filename, mime, size, kind, sha256 TEXT, extract_status, ocr_status, embedded_partial INTEGER DEFAULT 0, page_count, text_chars, PRIMARY KEY(message_key, ordinal))`, `chunks(id INTEGER PRIMARY KEY, message_key TEXT, part TEXT, idx INTEGER, text TEXT, token_count INTEGER, embedded INTEGER DEFAULT 0)`, `chunks_fts` (FTS5 external content on `chunks.text`, `tokenize = "unicode61 tokenchars '-_#@'"`, insert/delete/update triggers; the full stop is left out so a sentence-final `summary.` still matches `summary`), `chunks_trigram` (FTS5 `tokenize='trigram'`, same content and triggers), `chunks_vec` (`vec0(chunk_rowid INTEGER PRIMARY KEY, embedding float[384])`), `chains(chain_id TEXT PRIMARY KEY, root_message_key, subject, first_ts, last_ts, message_count, participants_json)`, `chain_summaries(chain_id, members_hash, conclusion, decisions_json, open_items_json, model, created_at)`, `jobs(id INTEGER PRIMARY KEY, kind, payload TEXT, state TEXT, priority INTEGER, attempts INTEGER, last_error TEXT, created_at, updated_at)`, `schema_version(version INTEGER)`; indexes on `messages(sent_ts)`, `messages(sender_addr)`, `messages(chain_id)`, `recipients(addr)`, `chunks(message_key)`, `files(message_key)`, `files(last_seen_scan_id)`, `jobs(state, priority)`, `attachments(sha256)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_connect_enables_wal_and_vec(tmp_path):
    c = connect(tmp_path / "p.sqlite"); assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and c.execute("SELECT vec_version()").fetchone()[0]

def test_migrate_creates_all_tables_and_records_version(tmp_path):
    c = connect(tmp_path / "p.sqlite"); assert migrate(c) == SCHEMA_VERSION
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"files","messages","recipients","attachments","chunks","chunks_fts","chunks_trigram","chunks_vec","chains","chain_summaries","jobs","settings","folders"} <= names
    assert schema_version(c) == SCHEMA_VERSION

def test_migrate_is_idempotent(tmp_path): c = connect(tmp_path / "p.sqlite"); migrate(c); assert migrate(c) == SCHEMA_VERSION

def test_migrate_refuses_newer_schema(tmp_path):
    c = connect(tmp_path / "p.sqlite"); migrate(c); c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,)); c.commit()
    with pytest.raises(SchemaTooNew): migrate(c)

def test_fts_triggers_keep_index_in_sync(tmp_path):
    c = connect(tmp_path / "p.sqlite"); migrate(c)
    c.execute("INSERT INTO chunks(message_key,part,idx,text,token_count) VALUES('m','body',0,'invoice INV-2024-00017 approved',5)")
    assert c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"INV-2024-00017\"'").fetchone()
    assert c.execute("SELECT rowid FROM chunks_trigram WHERE chunks_trigram MATCH '\"2024-000\"'").fetchone()
    c.execute("DELETE FROM chunks"); assert not c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'invoice'").fetchone()
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** `connection.py` (`enable_load_extension(True)`, `sqlite_vec.load(conn)`, `enable_load_extension(False)`, FTS5 check via `PRAGMA compile_options`) and `schema.py` with migration 1 as one SQL script string.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src/emailsupersearch/db tests/test_db.py && git commit -m "feat: sqlite connection with sqlite-vec and FTS5, schema v1 migrations"`

### Task 5: Doctor command and CLI skeleton

**Files:**
- Create: `src/emailsupersearch/diagnostics/__init__.py`, `src/emailsupersearch/diagnostics/doctor.py`, `src/emailsupersearch/cli.py`
- Test: `tests/test_doctor.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `@dataclass class Check: name: str; status: Literal["ok","warn","fail","skip"]; detail: str; remedy: str = ""`; `@dataclass class DoctorReport: checks: list[Check]; degraded: list[str]; generated_at: int`; `run_doctor(root: Path | None = None) -> DoctorReport`; `render_report(r: DoctorReport) -> str`; `report_json(r) -> str`; `cli.main(argv: list[str] | None = None) -> int` (wraps `parser.parse_args` in `try/except SystemExit as e: return int(e.code or 0)`); `cli.register(name: str, fn: Callable[[argparse.Namespace], int], configure: Callable[[argparse.ArgumentParser], None])` so later tasks add `serve`, `stop`, `bench`, `sync`, `purge`; `serve` is the default when no subcommand is given.
- Checks (one function each): `python_build` (version, `sys.prefix`, Store Python when the prefix contains `WindowsApps` or `PythonSoftwareFoundation`); `path_headroom` (warn when `len(str(site_packages)) > 130`); `crt_dlls` (`ctypes.WinDLL` of `msvcp140.dll`, `msvcp140_1.dll`, `vcruntime140_1.dll`, `vcomp140.dll`; remedy `pip install msvc-runtime`); `vulkan_loader` (`vulkan-1.dll` loads and `vkEnumerateInstanceVersion` returns a version; device enumeration is M2); `native_imports` (each of `onnxruntime`, `sqlite_vec`, `tokenizers`, `pypdfium2`, `PIL`, `watchfiles`, `winrt.windows.media.ocr`, `rapidocr`, `cv2` imported in `subprocess.run([sys.executable, "-c", ...], timeout=60)`; the child prints each module's `__file__` and the `.pyd`/`.dll` files in its package directory; the parent runs one `powershell -NoProfile -Command "Get-AuthenticodeSignature -FilePath <paths> | Select-Object Status, @{n='Subject';e={$_.SignerCertificate.Subject}} | ConvertTo-Json"` and records `signed:<subject>` or `unsigned` per file in `detail`; optional modules not installed → `skip`); `sqlite_features` (FTS5, `enable_load_extension`); `ocr_languages` (Windows OCR `OcrEngine.available_recognizer_languages`, `skip` off Windows); `memory_disk` (`ctypes` `GlobalMemoryStatusEx` and `shutil.disk_usage` of the data root); `data_root` (`validate_data_root`); `msg_handler` (HKCU `Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.msg\UserChoice` ProgId, else `warn`); `models_package` (`load_default_embedder()` is not None). `degraded` lists `"semantic search"` when `models_package` fails and `"ocr"` when both OCR engines are unavailable. `render_report` replaces `%USERPROFILE%` and `USERNAME` with `<user>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_doctor_has_every_check_and_no_crash():
    r = run_doctor(); names = {c.name for c in r.checks}
    assert names >= {"python_build","path_headroom","crt_dlls","vulkan_loader","native_imports","sqlite_features","ocr_languages","memory_disk","data_root","msg_handler","models_package"}
    assert all(c.status in {"ok","warn","fail","skip"} for c in r.checks)

def test_native_imports_reports_signer_status():
    d = [c for c in run_doctor().checks if c.name == "native_imports"][0].detail; assert "signed:" in d or "unsigned" in d

def test_render_report_redacts_username(): assert os.environ.get("USERNAME", "\x00") not in render_report(run_doctor())
def test_cli_doctor_exits_zero(capsys): assert main(["doctor"]) == 0 and "python_build" in capsys.readouterr().out
def test_cli_doctor_json(capsys): main(["doctor", "--json"]); assert json.loads(capsys.readouterr().out)["checks"]
def test_cli_version(capsys): assert main(["--version"]) == 0 and "0.0.1" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS; run `.venv/Scripts/python -m emailsupersearch doctor` by hand → a readable report.
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: doctor diagnostics with signer status and CLI skeleton"`

### Task 6: Shared types and message identity

**Files:**
- Create: `src/emailsupersearch/types.py`, `src/emailsupersearch/ingest/__init__.py`, `src/emailsupersearch/ingest/identity.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Produces in `types.py`: `AttachmentKind = Literal["pdf","docx","xlsx","pptx","image","msg","eml","other"]`; `AttachmentStatus = Literal["ok","needs_ocr","unsupported","failed","partial"]`; `@dataclass(frozen=True) class Person(name: str, addr: str)`; `@dataclass(frozen=True) class Recipient(kind: Literal["to","cc","bcc"], name: str, addr: str)`; `@dataclass class ParsedAttachment(ordinal: int, filename: str, mime: str, size: int, kind: AttachmentKind, data: bytes | None = None, nested: "ParsedMessage | None" = None, content_id: str | None = None)`; `@dataclass class ParsedMessage(message_id: str | None, subject: str, sender_name: str, sender_addr: str, recipients: list[Recipient], sent_ts: int | None, received_ts: int | None, date_source: str, body_text: str, body_html: str | None, in_reply_to: str | None, references: list[str], conversation_index: bytes | None, conversation_topic: str | None, attachments: list[ParsedAttachment])`; `@dataclass class AttachmentText(ordinal: int, filename: str, kind: AttachmentKind, size: int, status: AttachmentStatus, text: str, page_count: int, needs_ocr_pages: list[int], sha256: str | None = None, reason: str = "")`; `@dataclass class ParsedDocument(sha256: str, message: ParsedMessage, attachments: list[AttachmentText])`; `@dataclass class SearchFilters(query: str = "", from_addrs: list[str] = field(default_factory=list), recipient_addrs: list[str] = field(default_factory=list), date_from: int | None = None, date_to: int | None = None, attachment_kinds: list[str] = field(default_factory=list), has_attachments: bool | None = None, group_by_chain: bool = True, only_with_conclusion: bool = False, sort: Literal["relevance","date"] = "relevance")`.
- Produces in `identity.py`: `normalize_addr(s: str) -> str` (lower-case, strip angle brackets and whitespace; an X.500 `/O=.../CN=...` address is kept whole, lower-cased); `normalize_subject(s: str) -> str` (strip repeated `re:`, `fw:`, `fwd:`, `aw:`, `wg:` prefixes with optional `[n]`, collapse whitespace, lower-case); `message_key(m: ParsedMessage) -> str` (Message-ID without angle brackets when present, else `sha256(f"{normalize_addr(sender_addr)}|{sent_ts}|{normalize_subject(subject)}|{body_text[:2048]}")` hex).
- conftest: `parsed_factory(**overrides) -> ParsedMessage` with sensible defaults.

- [ ] **Step 1: Write the failing tests**

```python
def test_normalize_addr(): assert normalize_addr(" <Jane.Doe@Example.COM> ") == "jane.doe@example.com"
def test_normalize_subject_strips_reply_prefixes(): assert normalize_subject("RE: Re[2]: FW: Acme  renewal ") == "acme renewal"
def test_message_key_uses_message_id_when_present(parsed_factory): assert message_key(parsed_factory(message_id="<abc@x>")) == "abc@x"
def test_message_key_metadata_hash_is_stable_and_body_sensitive(parsed_factory):
    a = parsed_factory(message_id=None, body_text="hello"); b = parsed_factory(message_id=None, body_text="hello"); c = parsed_factory(message_id=None, body_text="other")
    assert message_key(a) == message_key(b) != message_key(c) and len(message_key(a)) == 64
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: shared types and message identity"`

### Task 7: Synthetic corpus module, EML adapter and test helpers

**Files:**
- Create: `src/emailsupersearch/diagnostics/corpus.py`, `src/emailsupersearch/ingest/eml_adapter.py`, `tests/helpers.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_corpus.py`, `tests/test_eml_adapter.py`

**Interfaces:**
- Produces in `corpus.py` (shipped in the wheel because `bench` uses it): `TOPICS: list[tuple[str, str, list[str]]]` with at least 12 entries of (subject, a 3-to-4-sentence root body about one distinct office topic such as printer lease renewal, Q3 budget approval, office move, VPN outage, two short reply sentences); `PEOPLE: list[Person]` (6 people across `example.com` and `example.org`); `generate(out_dir: Path, n_threads: int, seed: int) -> list[Path]` writing files named `t{thread:03d}-m{index:02d}.eml`, returned sorted, where thread `i` uses `TOPICS[i % len(TOPICS)]`, has 2 to 6 messages (`random.Random(seed)`), replies carry `In-Reply-To` and `References`, quote the previous body with `> ` prefixes and end with a signature block, the last message of thread `i` carries one attachment of kind `["pdf","docx","xlsx","png"][i % 4]`, and thread 0's root body starts with the sentence containing `INV-2024-00017`; all `Date` headers are in 2026 UTC spaced by hours; `.docx` and `.xlsx` bytes are made deterministic by rewriting every zip entry's `date_time` to `(2026,1,1,0,0,0)` (and `wb.properties.created = modified = datetime(2026,1,1)` for openpyxl); `write_minimal_pdf(text: str) -> bytes` (hand-written single-page PDF with one text object); `write_text_png(text: str, size=(1200, 400)) -> bytes` (Pillow default font, black on white); `make_eml_bytes(subject: str = "new", seed: int = 99) -> bytes` (one standalone message); `if __name__ == "__main__": generate(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1)`.
- Produces in `eml_adapter.py`: `parse_eml(path: Path) -> ParsedMessage` (stdlib `email` with `policy=default`; `date_source` one of `"header_date"`, `"mtime"`; `sent_ts` UTC epoch int; attachments via `iter_attachments()` with `kind` from `kind_for` (Task 9; until then a local suffix map); nested `message/rfc822` parts recursed into `nested`; parts with `Content-ID` get `content_id`).
- `tests/helpers.py`: re-exports `make_eml_bytes`, `write_minimal_pdf`, `write_text_png`; `wait_until(pred: Callable[[], Any], timeout_s: float, interval: float = 0.1)` (returns the truthy value or `pytest.fail`); `LONG_TEXT_3000_WORDS` and `LONG_TEXT_30000_WORDS` (sentences of 8 to 15 words from a fixed word list with `random.Random(11)`, joined by `". "`, a blank line every 6 sentences); attachment builders `att_pdf(data) / att_docx(text) / att_xlsx(rows) / att_pptx(text) / att_png(data)` returning `ParsedAttachment`; `blank_pdf_bytes(pages: int)` (PDF with empty pages); `noisy_png(min_bytes: int) -> bytes` (seeded random RGB noise with text drawn on top, enlarged until the PNG exceeds `min_bytes`); `encrypted_pdf_bytes()` (`pypdf.PdfWriter`, `add_blank_page(200, 100)`, `encrypt("secret")`).
- conftest: `corpus_dir(tmp_path) -> Path` = directory containing `generate(tmp_path / "corpus", 6, seed=1)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_generate_corpus_is_deterministic(tmp_path):
    a = generate(tmp_path / "a", 4, seed=7); b = generate(tmp_path / "b", 4, seed=7)
    assert [p.name for p in a] == [p.name for p in b] and all(x.read_bytes() == y.read_bytes() for x, y in zip(a, b))

def test_generate_covers_all_attachment_kinds(tmp_path):
    kinds = {a.kind for p in generate(tmp_path, 4, seed=3) for a in parse_eml(p).attachments}
    assert kinds == {"pdf","docx","xlsx","image"}

def test_thread_zero_root_has_invoice_number(tmp_path):
    assert "INV-2024-00017" in parse_eml(generate(tmp_path, 1, seed=1)[0]).body_text

def test_parse_eml_fields(tmp_path):
    m = parse_eml(generate(tmp_path, 1, seed=1)[0])
    assert m.message_id and m.sender_addr.endswith(("example.com", "example.org")) and m.sent_ts and m.date_source == "header_date"
    assert any(r.kind == "to" for r in m.recipients) and m.body_text.strip()

def test_parse_eml_replies_have_in_reply_to(tmp_path):
    paths = generate(tmp_path, 1, seed=2); assert all(parse_eml(p).in_reply_to for p in paths[1:])

def test_parse_eml_nested_rfc822(tmp_path):
    p = tmp_path / "n.eml"; outer = EmailMessage(); outer["Subject"] = "outer"; outer["From"] = "a@example.com"; outer["To"] = "b@example.com"; outer.set_content("hi")
    inner = EmailMessage(); inner["Subject"] = "inner"; inner.set_content("x"); outer.add_attachment(inner); p.write_bytes(outer.as_bytes())
    m = parse_eml(p); assert m.attachments[0].kind == "eml" and m.attachments[0].nested.subject == "inner"
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** `corpus.py`, `parse_eml`, `tests/helpers.py` and the `corpus_dir` fixture.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: deterministic synthetic corpus, eml adapter and test helpers"`

### Task 8: MSG adapter with extract-msg

**Files:**
- Create: `src/emailsupersearch/ingest/msg_adapter.py`, `src/emailsupersearch/ingest/parse.py`, `scripts/fetch_fixtures.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_msg_adapter.py`

**Interfaces:**
- Produces: `parse_msg(path: Path) -> ParsedMessage`; `_convert(msg: extract_msg.MessageBase, depth: int) -> ParsedMessage` (the shared converter; `parse_msg` opens the file and calls it with depth 0); `parse_file(path: Path) -> ParsedMessage` dispatching on suffix (`.msg`, `.eml`) and raising `ParseError(reason: str)` on anything else or on adapter exceptions; `MSG_CONVERSATION_INDEX_STREAM = "__substg1.0_00710102"`, `MSG_CONVERSATION_TOPIC_STREAM = "__substg1.0_0070"`, `MSG_ATTACH_CONTENT_ID_STREAM = "__substg1.0_3712"`.
- `parse_msg` opens with `extract_msg.openMsg(path, delayAttachments=True)` inside `with`; on `UnicodeDecodeError` retries with `overrideEncoding="chardet"`; `body_text = msg.body`, falling back to a regex strip of `msg.rtfBody` control words; `sent_ts` from `msg.date`, else `msg.receivedTime`, else header `Date`, else mtime, with `date_source` in `{"sent","received","header_date","mtime"}`; recipients from `msg.recipients` (type → kind); `message_id = msg.messageId`; `in_reply_to = msg.inReplyTo`; `references` from `msg.header["References"]`; `conversation_index = msg.getStream(MSG_CONVERSATION_INDEX_STREAM)`; `conversation_topic = msg.getStringStream(MSG_CONVERSATION_TOPIC_STREAM)`; attachments: `AttachmentType.DATA` → bytes with `content_id` from `att.getStringStream(MSG_ATTACH_CONTENT_ID_STREAM)`; `AttachmentType.MSG` → `nested=_convert(att.data, depth + 1)` when `depth < 3` (`att.data` is already an opened message object); other types → kind `other`, `data=None`.
- `scripts/fetch_fixtures.py`: lists `example-msg-files` in `TeamMsgExtractor/msg-extractor` at tag `v0.56.1` via the GitHub contents API and downloads every `.msg` into `tests/fixtures/msg/`. conftest `msg_fixtures` yields the sorted list of files under `tests/fixtures/msg` and `tests/fixtures/owner`, skipping with "run scripts/fetch_fixtures.py" when both are empty.

- [ ] **Step 1: Write `scripts/fetch_fixtures.py` as in Interfaces and run it** (`.venv/Scripts/python scripts/fetch_fixtures.py`) → `.msg` files under `tests/fixtures/msg/` (five at the time of writing; none is RTF-only, so Review Focus 1 is fully exercised only once the owner's samples are in `tests/fixtures/owner/`).

- [ ] **Step 2: Write the failing tests**

```python
def test_parse_msg_every_fixture_has_subject_or_body(msg_fixtures):
    for p in msg_fixtures: m = parse_msg(p); assert m.subject or m.body_text, p.name

def test_parse_msg_rtf_only_body_has_text(msg_fixtures):
    for p in msg_fixtures:
        m = parse_msg(p)
        if m.body_html is None and m.body_text == "": pytest.fail(f"{p.name}: no body text recovered")

def test_parse_msg_dates_are_utc_ints_with_source(msg_fixtures):
    for p in msg_fixtures: m = parse_msg(p); assert m.sent_ts is None or (isinstance(m.sent_ts, int) and m.date_source in {"sent","received","header_date","mtime"})

def test_parse_msg_releases_file_handle(msg_fixtures, tmp_path):
    dst = tmp_path / msg_fixtures[0].name; shutil.copy(msg_fixtures[0], dst); parse_msg(dst); dst.unlink()

def test_parse_file_rejects_unknown_suffix(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(ParseError): parse_file(tmp_path / "x.txt")
```

- [ ] **Step 3: Run to verify failure** → FAIL
- [ ] **Step 4: Implement** the adapter and dispatcher.
- [ ] **Step 5: Run to verify pass** → PASS
- [ ] **Step 6: Commit** — `git add src scripts/fetch_fixtures.py tests && git commit -m "feat: msg adapter over extract-msg with conversation index and parse dispatch"`

### Task 9: Attachment text extraction matrix

**Files:**
- Create: `src/emailsupersearch/ingest/attachments.py`
- Modify: `src/emailsupersearch/ingest/eml_adapter.py` (switch its interim suffix map to `kind_for`)
- Test: `tests/test_attachments.py`

**Interfaces:**
- Produces: `MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024`; `INLINE_IMAGE_MIN_BYTES = 100 * 1024`; `LOW_TEXT_CHARS_PER_PAGE = 20`; `MAX_OCR_PAGES = 50`; `MAX_XLSX_CELLS = 50_000`; `IMAGE_SUFFIXES = {".png",".jpg",".jpeg",".tif",".tiff",".bmp",".webp"}`; `kind_for(filename: str, mime: str) -> AttachmentKind` (suffix first, with `.xlsm` counted as `xlsx`; MIME fallback when the suffix is missing or unknown: `message/rfc822` → `eml`, `application/vnd.ms-outlook` → `msg`, `image/*` → `image`, `application/pdf` → `pdf`); `extract_text(att: ParsedAttachment) -> AttachmentText` which never raises: size over the cap → `unsupported`, reason `too_large`, without reading; `image` with `content_id` set and `size < INLINE_IMAGE_MIN_BYTES` → `unsupported`, reason `inline_image`; `pdf` via pypdfium2 text per page, pages under 20 characters listed in `needs_ocr_pages` (capped at `MAX_OCR_PAGES`) and status `needs_ocr` when any; `pypdfium2.PdfiumError` with `"password"` in `str(e).lower()` → `unsupported`, reason `encrypted`, any other exception → `failed` with the exception class name; `docx` via python-docx paragraphs, tables, headers and footers; `xlsx` via openpyxl `read_only=True`, each sheet name prepended to its cell values, `partial` after `MAX_XLSX_CELLS`; `pptx` via python-pptx text frames, tables and notes; `image` → `needs_ocr` with `needs_ocr_pages=[0]`; `msg`/`eml` → the nested message's subject and body; `other` → `unsupported`, reason `unsupported_type`; `sha256` of `data` filled whenever data is present; `render_pdf_page_png(data: bytes, page_index: int, dpi: int = 200) -> bytes`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pdf_text_extracted(): assert "INV-2024-00017" in extract_text(att_pdf(write_minimal_pdf("Invoice INV-2024-00017"))).text
def test_scanned_pdf_pages_need_ocr(): r = extract_text(att_pdf(blank_pdf_bytes(pages=2))); assert r.status == "needs_ocr" and r.needs_ocr_pages == [0, 1]
def test_docx_xlsx_pptx_text():
    assert "quarterly" in extract_text(att_docx("quarterly figures")).text.lower()
    x = extract_text(att_xlsx([["Budget", 412000]])).text; assert "412000" in x and "Sheet" in x
    assert "kickoff" in extract_text(att_pptx("Kickoff")).text.lower()
def test_image_needs_ocr(): assert extract_text(att_png(write_text_png("hello"))).status == "needs_ocr"
def test_small_cid_image_skipped_large_cid_image_ocrd():
    small = att_png(write_text_png("logo")); small.content_id = "logo@x"; assert extract_text(small).reason == "inline_image"
    big = att_png(noisy_png(INLINE_IMAGE_MIN_BYTES + 1)); big.content_id = "scan@x"; assert big.size > INLINE_IMAGE_MIN_BYTES and extract_text(big).status == "needs_ocr"
def test_oversized_attachment_flagged_unsupported_too_large():
    r = extract_text(ParsedAttachment(0, "big.pdf", "application/pdf", MAX_ATTACHMENT_BYTES + 1, "pdf", data=None)); assert (r.status, r.reason) == ("unsupported", "too_large")
def test_encrypted_pdf_flagged_unsupported(): r = extract_text(att_pdf(encrypted_pdf_bytes())); assert (r.status, r.reason) == ("unsupported", "encrypted")
def test_kind_for_xlsm_and_images(): assert kind_for("a.xlsm", "") == "xlsx" and kind_for("b.webp", "") == "image"
def test_render_pdf_page_png_returns_png(): assert render_pdf_page_png(write_minimal_pdf("x"), 0)[:8] == b"\x89PNG\r\n\x1a\n"
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: attachment text extraction matrix with caps and statuses"`

### Task 10: Content hygiene

**Files:**
- Create: `src/emailsupersearch/ingest/hygiene.py`
- Test: `tests/test_hygiene.py`

**Interfaces:**
- Produces: `strip_quoted(body: str) -> str` (removes from the first of: a line matching `^On .+ wrote:$`, a `^From: .+$` line followed within 4 lines by `^Sent: ` or `^To: `, `^-----Original Message-----`, a run of `^>` lines, Outlook's `________________________________` separator); `strip_signature(body: str) -> str` (removes from `^-- $`, `^(Kind|Best|Warm) regards,?$`, `^Thanks,?$`, `^Sent from my `, or a trailing block of at most 6 short lines containing a phone number); `new_text(body: str, has_parent: bool) -> str` (quote stripping only when `has_parent`, signature stripping always, whitespace collapsed; never empty when the input had text).

- [ ] **Step 1: Write the failing tests**

```python
def test_strip_quoted_outlook_header_block():
    body = "Agreed, 4% is fine.\n\nFrom: Priya Nair\nSent: Monday\nTo: Daniel\nSubject: RE: Acme\n\nEarlier text"; assert strip_quoted(body).strip() == "Agreed, 4% is fine."
def test_strip_quoted_on_wrote_and_gt_lines(): assert strip_quoted("Yes.\n\nOn Tue, Mar 3 Priya wrote:\n> old\n> text").strip() == "Yes."
def test_strip_signature_regards_block(): assert strip_signature("Done.\n\nKind regards,\nMei Lin\nFinance | +44 20 7946 0000").strip() == "Done."
def test_new_text_keeps_quotes_without_parent(): assert "old" in new_text("Yes.\n> old", has_parent=False) and "old" not in new_text("Yes.\n> old", has_parent=True)
def test_new_text_never_empty(): assert new_text("> only a quote", has_parent=True).strip()
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** with compiled regexes.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: quoted-reply and signature stripping"`

### Task 11: Chunker

**Files:**
- Create: `src/emailsupersearch/ingest/chunker.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_chunker.py`

**Interfaces:**
- Produces: `CHUNKER_VERSION = 1`; `MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT = 64`; `@dataclass class ChunkText(idx: int, text: str, token_count: int)`; `class Chunker: __init__(tokenizer_path: Path | None, target_tokens: int = 220, overlap_ratio: float = 0.15, hard_max_tokens: int = 256)` (when `tokenizer_path` is None, tokens are whitespace-separated words; the keyword-only install uses this); `chunk(text: str, context_line: str) -> list[ChunkText]` (each chunk's text is `context_line + "\n" + piece`; split on blank lines, then sentences, then words; greedy packing up to `target_tokens`; the next chunk starts with at least the last sentence of the previous chunk plus earlier ones while the overlap stays under `overlap_ratio * target_tokens`; any single sentence over `hard_max_tokens` is split by words); `context_line_for(sender: str, sent_ts: int | None, subject: str, part_label: str) -> str` (`From {sender} on {YYYY-MM-DD}: {subject} [{part_label}]` with the date from `datetime.fromtimestamp(sent_ts, timezone.utc)`, or `unknown date`).
- conftest: `chunker` fixture built from `tokenizer_path(embedding_dir())`, skipping when the model files are missing; `chunker_ws` = `Chunker(None)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_chunks_respect_token_bounds(chunker):
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "From a on 2026-03-01: s [body]")
    assert len(out) > 5 and all(c.token_count <= 256 for c in out) and all(c.text.startswith("From a on") for c in out)
def test_chunks_overlap(chunker):
    out = chunker.chunk(LONG_TEXT_3000_WORDS, "ctx"); tail = " ".join(out[0].text.split()[-5:]); assert tail in out[1].text
def test_short_text_is_one_chunk(chunker): assert len(chunker.chunk("Short note.", "ctx")) == 1
def test_long_text_yields_many_chunks(chunker): assert len(chunker.chunk(LONG_TEXT_30000_WORDS, "ctx")) > 64
def test_empty_text_gives_no_chunks(chunker): assert chunker.chunk("   ", "ctx") == []
def test_whitespace_chunker_works_without_model(chunker_ws): assert len(chunker_ws.chunk(LONG_TEXT_3000_WORDS, "ctx")) > 5
def test_context_line_format(): assert context_line_for("Priya Nair", 1773705600, "RE: Acme", "body") == "From Priya Nair on 2026-03-17: RE: Acme [body]"
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: token-aware chunker with overlap and whitespace fallback"`

### Task 12: Parse cache and parse worker pool

**Files:**
- Create: `src/emailsupersearch/ingest/parse_cache.py`, `src/emailsupersearch/ingest/workers.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_parse_cache.py`, `tests/test_workers.py`

**Interfaces:**
- Produces: `class ParseCache: __init__(cache_dir: Path)`; `get(sha256: str) -> ParsedDocument | None`; `put(doc: ParsedDocument) -> None` (JSON at `cache_dir/<sha256>.json`, the flat layout of spec 6.2; attachment `data` never stored; `conversation_index` as hex); `set_ocr_text(sha256: str, ordinal: int, text: str) -> None` (status becomes `ok`, `needs_ocr_pages` cleared); `sha256_file(path: Path) -> str`.
- Produces: `parse_document(path: Path) -> ParsedDocument` (module-level: `parse_file` + `extract_text` per attachment; nested messages flattened into the attachment list as `msg`/`eml` text); `class ParseTimeout(Exception)`, `class ParseMemoryExceeded(Exception)`; `class ParsePool: __init__(workers: int = 1, timeout_s: float = 120.0, max_rss_mb: int = 1024)`; `parse(path: Path) -> ParsedDocument` (submits the module-level `_worker_fn(path, max_rss_mb)`, which records the process working set at entry via `GetProcessMemoryInfo`, parses, and raises `ParseMemoryExceeded` when the peak working set grew by more than `max_rss_mb` over that baseline; on `concurrent.futures.TimeoutError` or `ParseMemoryExceeded` the pool kills every worker process (`for p in list(executor._processes.values()): p.kill()`), calls `executor.shutdown(wait=False, cancel_futures=True)`, recreates the executor so an inflated worker never serves the next file, and raises `ParseTimeout(path)` or re-raises `ParseMemoryExceeded`); `close()`. The executor uses the `spawn` context; `_worker_fn` is looked up at submit time so a test can monkeypatch it.
- conftest: `eml_doc` = `parse_document(generate(tmp_path, 1, seed=1)[-1])` (the thread's last message, which carries the PDF attachment); `eml_doc_with_image` = `parse_document` of the file in `generate(tmp_path, 4, seed=3)` whose attachment kind is `image`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cache_roundtrip(tmp_path, eml_doc):
    c = ParseCache(tmp_path); assert c.get(eml_doc.sha256) is None; c.put(eml_doc)
    back = c.get(eml_doc.sha256); assert back.message.subject == eml_doc.message.subject and back.attachments[0].text == eml_doc.attachments[0].text
def test_cache_set_ocr_text(tmp_path, eml_doc_with_image):
    c = ParseCache(tmp_path); c.put(eml_doc_with_image); c.set_ocr_text(eml_doc_with_image.sha256, 0, "recovered words")
    a = c.get(eml_doc_with_image.sha256).attachments[0]; assert (a.status, a.text, a.needs_ocr_pages) == ("ok", "recovered words", [])
def test_parse_pool_parses_eml(tmp_path):
    p = generate(tmp_path, 1, seed=1)[0]; pool = ParsePool(); doc = pool.parse(p); assert doc.message.subject and doc.sha256 == sha256_file(p); pool.close()
def test_parse_pool_times_out_and_recovers(tmp_path, monkeypatch):
    pool = ParsePool(timeout_s=0.5); monkeypatch.setattr(workers, "_worker_fn", workers._sleep_forever_for_tests)
    with pytest.raises(ParseTimeout): pool.parse(tmp_path / "any.eml")
    monkeypatch.undo(); assert pool.parse(generate(tmp_path, 1, seed=1)[0]).message.subject; pool.close()
def test_parse_pool_memory_cap_fails_file_not_pool(tmp_path, monkeypatch):
    pool = ParsePool(max_rss_mb=64); monkeypatch.setattr(workers, "_worker_fn", workers._allocate_for_tests)  # allocates 256 MB over the baseline, then applies the same growth check
    with pytest.raises(ParseMemoryExceeded): pool.parse(tmp_path / "any.eml")
    monkeypatch.undo(); assert pool.parse(generate(tmp_path, 1, seed=1)[0]).message.subject; pool.close()
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**; serialise `ParsedDocument` with `dataclasses.asdict` plus explicit hex for bytes; the two `_..._for_tests` functions live in `workers.py` so they are importable by spawned processes.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: shared parse cache and subprocess parse pool with timeout and memory cap"`

### Task 13: Vector index (sqlite-vec)

**Files:**
- Create: `src/emailsupersearch/index/vectors.py`
- Test: `tests/test_vectors.py`

**Interfaces:**
- Produces: `class VectorIndex: __init__(conn: sqlite3.Connection, dim: int = 384)`; `upsert(rows: list[tuple[int, np.ndarray]]) -> None` (delete then insert, `sqlite_vec.serialize_float32`); `delete(chunk_rowids: list[int]) -> None`; `knn(query: np.ndarray, k: int, candidates: list[int] | None) -> list[tuple[int, float]]` (rowid, similarity in `[0,1]` = `1 - distance/2` for normalised vectors; SQL `WHERE embedding MATCH ? AND k = ?` plus `AND chunk_rowid IN (...)` when candidates are given; `[]` when candidates is an empty list); `count() -> int`. The table `chunks_vec` comes from the Task 4 schema.

- [ ] **Step 1: Write the failing tests** — `test_upsert_and_count`, `test_knn_returns_nearest_first` (three near-orthogonal vectors), `test_knn_candidates_filter` (the nearest excluded by candidates is absent), `test_knn_empty_candidates_returns_empty`, `test_delete_removes_rows`, `test_knn_on_empty_index_returns_empty`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: sqlite-vec vector index with candidate filtering"`

### Task 14: Bench command (M0 probe complete)

**Files:**
- Create: `src/emailsupersearch/diagnostics/bench.py`
- Modify: `src/emailsupersearch/cli.py`, `README.md`
- Test: `tests/test_bench.py`

**Interfaces:**
- Produces: `@dataclass class BenchReport(parse_files_per_s: float, embed_chunks_per_s: float | None, ocr_pages_per_s: float | None, knn_ms_p50: float | None, knn_ms_p95: float | None, knn_vectors: int, peak_rss_mb: float, n_files: int, n_chunks: int, notes: list[str])`; `run_bench(corpus_dir: Path | None, n_threads: int = 50, seed: int = 1, vectors: int = 300_000) -> BenchReport` (uses `emailsupersearch.diagnostics.corpus.generate` into a temp dir when no corpus is given; parses with `parse_document`; chunks with `Chunker(tokenizer_path or None)` and embeds when `load_default_embedder()` returns one, else `None` fields and a note; `ocr_pages_per_s` over the corpus PNGs through `select_engine("en")` when available (Task 20; until then `None` with a note); kNN p50/p95 over 50 random queries against an in-memory `VectorIndex` padded with random normalised vectors to `vectors` rows); CLI `bench [--corpus DIR] [--vectors N] [--json]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_bench_without_models_has_notes(monkeypatch):
    monkeypatch.setattr("emailsupersearch.diagnostics.bench.load_default_embedder", lambda threads=4: None)
    r = run_bench(None, n_threads=3, vectors=1000); assert r.parse_files_per_s > 0 and r.embed_chunks_per_s is None and r.notes
def test_run_bench_with_models(embedder): r = run_bench(None, n_threads=3, vectors=5000); assert r.embed_chunks_per_s > 0 and r.knn_ms_p95 >= r.knn_ms_p50 > 0 and r.knn_vectors == 5000
def test_cli_bench_json(capsys, embedder): assert main(["bench", "--json", "--vectors", "1000"]) == 0 and json.loads(capsys.readouterr().out)["n_files"] > 0
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**; peak RSS via `GetProcessMemoryInfo` on Windows. Add the README section "Probe (M0)": the install from spec 7.3 with the Global Constraints install line, then `python -m emailsupersearch doctor` and `python -m emailsupersearch bench --json` with "paste both outputs back".
- [ ] **Step 4: Run to verify pass** → PASS; build the probe: `.venv/Scripts/python -m build --wheel` → `dist/emailsupersearch-0.0.1-py3-none-any.whl`; install it into a fresh `py -3.12 -m venv /tmp/probe` with the install line and run `doctor` and `bench --json` there.
- [ ] **Step 5: Commit and tag** — `git add src tests README.md && git commit -m "feat: bench command; M0 probe wheel" && git tag v0.0.1`

### Task 15: ProjectStore (single-writer facade)

**Files:**
- Create: `src/emailsupersearch/store.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `@dataclass class FileRecord(path: str, size: int, mtime_ns: int, sha256: str | None, file_id: str | None, message_key: str | None, parse_status: str, status_reason: str, last_seen_scan_id: int, tombstoned_at: int | None)`; `class ProjectStore: @classmethod open(project_dir: Path) -> ProjectStore` (connects `project.sqlite`, migrates; `SchemaTooNew` propagates); `conn`; `close()` (so the file can be deleted on Windows); every write method runs inside `with self._lock, self.conn:` on a `threading.RLock`, because the supervisor thread and request handlers share the connection; settings `get_setting(key, default=None)`, `set_setting(key, value)` (JSON values); folders `list_folders() -> list[str]`, `add_folder(path: str, kind: Literal["local","network"])`, `remove_folder(path)`; manifest `get_file(path) -> FileRecord | None`, `upsert_file(rec)`, `stamp_seen(paths: list[str], scan_id: int)`, `unseen_under(roots: list[str], scan_id: int) -> list[FileRecord]`, `tombstone(paths: list[str], now: int)`, `purge_tombstoned_under(roots: list[str], scan_id: int) -> list[FileRecord]` (deletes and returns rows under the given roots with `tombstoned_at IS NOT NULL AND last_seen_scan_id < scan_id - 1`), `paths_for_message(message_key, live_only: bool = True) -> list[str]`, `find_by_file_id(file_id) -> FileRecord | None`; messages `upsert_message(m: ParsedMessage, message_key: str, new_text: str, chain_id: str | None, chain_depth: int)` (replaces recipients), `upsert_attachments(message_key, atts: list[AttachmentText], ocr_status_default: str)`, `set_attachment_status(message_key, ordinal, extract_status: str | None = None, ocr_status: str | None = None, embedded_partial: bool | None = None)`, `delete_message(message_key)` (cascades recipients, attachments, chunks, FTS rows via triggers, and `chunks_vec` rows explicitly), `message(message_key) -> sqlite3.Row | None`; chunks `replace_chunks(message_key, part: str, chunks: list[ChunkText]) -> list[int]`, `mark_embedded(rowids: list[int])`, `chunk_rowids(message_key) -> list[int]`; `counts() -> dict[str, int]` with keys `messages`, `chunks`, `files`, `tombstoned`, `metadata_only` (messages with zero chunks, or whose file row has `parse_status in ('failed','indexed_partial')`).
- conftest: `tmp_store(tmp_path, corpus_dir)` = `ProjectStore.open(tmp_path / "proj")` with `add_folder(str(corpus_dir), "local")`.

- [ ] **Step 1: Write the failing tests** — `test_open_creates_and_migrates`, `test_settings_roundtrip_json`, `test_folders_crud`, `test_file_manifest_scan_lifecycle` (seen in scan 1, unseen in scan 2 → tombstoned; `paths_for_message` default excludes it, `live_only=False` includes it; unseen in scan 3 → `purge_tombstoned_under([root], 3)` returns it), `test_close_releases_file` (after `close()` the sqlite file can be unlinked), `test_upsert_message_replaces_recipients`, `test_replace_chunks_returns_rowids_and_deletes_old`, `test_delete_message_removes_chunks_and_vec_rows`, `test_counts_metadata_only`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** with explicit transactions (`with self.conn:`).
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: ProjectStore manifest, messages and chunks"`

### Task 16: Keyword index (FTS5 queries)

**Files:**
- Create: `src/emailsupersearch/index/fts.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_fts.py`

**Interfaces:**
- Produces: `sanitize_query(q: str) -> str` (each whitespace token becomes a double-quoted phrase with inner quotes removed; empty tokens dropped; `""` when nothing remains); `looks_like_identifier(q: str) -> bool` (a single token containing a digit and one of `-`, `_`, `#`, `/`, or at least 6 characters mixing letters and digits); `bm25_search(conn, q: str, k: int, candidates: list[int] | None) -> list[tuple[int, float]]` (score = `-bm25(chunks_fts)` so higher is better); `trigram_search(conn, term: str, k: int, candidates: list[int] | None) -> list[tuple[int, float]]`; `keyword_search(conn, q, k, candidates) -> list[tuple[int, float]]` (bm25 plus trigram when `looks_like_identifier`, merged by max score). All three return `[]` without executing SQL when the sanitised query is empty, when the trigram term is shorter than 3 characters, or when `candidates` is an empty list; a candidate list longer than 900 goes through a temp table.
- conftest: `store_with_chunks(tmp_store)` inserts five chunks (the first three contain the word `renewal`; the fourth contains `INV-2024-00017`) and exposes `.rowids` (insertion order) and `.invoice_rowid`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("q", ['"unterminated', "a*b", "col:val", "-neg (x", "NEAR(a b)", "   ", ""])
def test_sanitize_query_never_raises(store_with_chunks, q): keyword_search(store_with_chunks.conn, q, 10, None)
def test_bm25_ranks_exact_identifier_first(store_with_chunks): rows = bm25_search(store_with_chunks.conn, "INV-2024-00017", 10, None); assert rows and rows[0][0] == store_with_chunks.invoice_rowid
def test_trigram_finds_partial_identifier(store_with_chunks): assert store_with_chunks.invoice_rowid in [r for r, _ in trigram_search(store_with_chunks.conn, "2024-000", 10, None)]
def test_candidates_restrict_results(store_with_chunks):
    allowed = store_with_chunks.rowids[:2]; rows = bm25_search(store_with_chunks.conn, "renewal", 10, allowed); assert rows and all(r in allowed for r, _ in rows)
def test_empty_candidates_return_nothing(store_with_chunks): assert keyword_search(store_with_chunks.conn, "renewal", 10, []) == []
def test_looks_like_identifier(): assert looks_like_identifier("INV-2024-00017") and not looks_like_identifier("renewal terms")
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: FTS5 keyword search with sanitising and trigram identifiers"`

### Task 17: Chains

**Files:**
- Create: `src/emailsupersearch/index/chains.py`
- Test: `tests/test_chains.py`

**Interfaces:**
- Produces: `thread_guid(conversation_index: bytes | None) -> str | None` (hex of bytes 6..22 when length ≥ 22); `chain_depth(conversation_index: bytes | None) -> int` (`(len-22)//5`, 0 when None or short); `assign_chain(store: ProjectStore, m: ParsedMessage, message_key: str) -> tuple[str, int]` in this order: `thread_guid`; else the chain of the message whose `message_id` equals `in_reply_to` or any of `references`; else an existing chain with the same `normalize_subject` sharing at least one participant address within 30 days of `sent_ts`; else a new chain id `"c-" + message_key[:16]`; `update_chain_row(store, chain_id) -> None` (recomputes subject, first/last ts, count, participants JSON from `messages`; deletes the `chains` row when the count is 0).

- [ ] **Step 1: Write the failing tests** — `test_thread_guid_and_depth_from_synthetic_index` (22 + 5*2 bytes), `test_assign_chain_by_in_reply_to` (two messages of a generated thread), `test_assign_chain_by_subject_and_participants_within_30_days`, `test_unrelated_message_gets_new_chain`, `test_update_chain_row_counts_and_deletes_empty_chain`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: chain detection from conversation index, headers and subject"`

### Task 18: Reconcile scan

**Files:**
- Create: `src/emailsupersearch/ingest/scan.py`
- Test: `tests/test_scan.py`

**Interfaces:**
- Produces: `MAIL_SUFFIXES = {".msg", ".eml"}`; `@dataclass class ScanResult(scan_id: int, to_parse: list[Path], renamed: list[tuple[str, str]], unchanged: int, tombstoned: list[str], purged: list[FileRecord], aborted_roots: list[str], errors: list[tuple[str, str]])`; `reconcile(store: ProjectStore, roots: list[Path], scan_id: int, now: int) -> ScanResult` implementing spec 9.1: a root that is not a directory, or whose `scandir` raises, goes to `aborted_roots` and none of its rows are tombstoned; for each mail file: new → hash → if a tombstoned or unseen row with the same `file_id` exists, record a rename and move the row (the scan owns the move), else `to_parse`; existing with changed `(size, mtime_ns)` → rehash → `to_parse` only when the hash changed; an existing row whose `parse_status` is `pending` goes to `to_parse` even when unchanged (this is how a re-index re-parses every file from the cache); every seen path stamped; after the walk, rows under non-aborted roots not seen in this scan are tombstoned; `purge_tombstoned_under(non-aborted roots, scan_id)` supplies `purged`; the scan upserts manifest rows itself, parsing is the pipeline's job; `file_identity(path) -> str` (`f"{st_dev}:{st_ino}"`).

- [ ] **Step 1: Write the failing tests**

```python
def test_first_scan_lists_all_mail_files(tmp_store, corpus_dir): r = reconcile(tmp_store, [corpus_dir], 1, now=1); assert len(r.to_parse) == len(list(corpus_dir.glob("*.eml")))
def test_unchanged_second_scan_parses_nothing(tmp_store, corpus_dir): reconcile(tmp_store, [corpus_dir], 1, 1); r2 = reconcile(tmp_store, [corpus_dir], 2, 2); assert r2.to_parse == [] and r2.unchanged > 0
def test_modified_file_is_reparsed_only_if_bytes_changed(tmp_store, corpus_dir):  # os.utime only -> not in to_parse; append a byte -> in to_parse
def test_rename_is_detected_without_reparse(tmp_store, corpus_dir): ...; os.rename(a, b); r = reconcile(...); assert r.renamed == [(str(a), str(b))] and b not in r.to_parse
def test_deleted_file_tombstoned_then_purged_on_next_scan(tmp_store, corpus_dir): ...; assert r2.tombstoned == [str(victim)] and r3.purged[0].path == str(victim)
def test_missing_root_aborts_without_tombstones(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1); r = reconcile(tmp_store, [corpus_dir / "gone"], 2, 2); assert r.aborted_roots and r.tombstoned == [] and tmp_store.counts()["tombstoned"] == 0
def test_non_mail_files_ignored(tmp_store, corpus_dir): (corpus_dir / "notes.txt").write_text("x"); assert all(p.suffix in MAIL_SUFFIXES for p in reconcile(tmp_store, [corpus_dir], 1, 1).to_parse)
def test_pending_rows_are_reparsed_even_if_unchanged(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1); tmp_store.conn.execute("UPDATE files SET parse_status='pending'"); tmp_store.conn.commit()
    assert len(reconcile(tmp_store, [corpus_dir], 2, 2).to_parse) == len(list(corpus_dir.glob("*.eml")))
def test_aborted_root_keeps_old_tombstones(tmp_store, corpus_dir):
    reconcile(tmp_store, [corpus_dir], 1, 1); victim = sorted(corpus_dir.glob("*.eml"))[0]; victim.unlink(); reconcile(tmp_store, [corpus_dir], 2, 2)
    r = reconcile(tmp_store, [corpus_dir / "gone"], 3, 3); assert r.purged == [] and tmp_store.counts()["tombstoned"] == 1
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** with `os.scandir` recursion and `sha256_file` from Task 12.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: reconcile scan with rename detection and two-phase deletes"`

### Task 19: Ingest pipeline

**Files:**
- Create: `src/emailsupersearch/ingest/pipeline.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `@dataclass class IngestOutcome(path: str, message_key: str | None, status: Literal["indexed","indexed_partial","failed","skipped_duplicate"], reason: str = "", chunks: int = 0, ocr_jobs: int = 0)`; `class IngestPipeline: __init__(store: ProjectStore, cache: ParseCache, pool: ParsePool, chunker: Chunker, embedder: OnnxEmbedder | None, vectors: VectorIndex | None, enqueue_ocr: Callable[[dict], int] | None = None)`; `ingest_file(path: Path, scan_id: int) -> IngestOutcome` (sha256 → cache hit or `pool.parse`; `message_key`; if the key already exists with another live path, link the path and return `skipped_duplicate`; `assign_chain`; `new_text` with `has_parent` = the chain already has an earlier message; `upsert_message`; `upsert_attachments` with `ocr_status_default = "pending"` when `enqueue_ocr` is set else `"unavailable"`; chunks: body from `new_text` with its context line, each attachment as part `att:{ordinal}`; every chunk is stored (keyword-indexed); when an embedder exists, only the first `MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT` chunks of each attachment part are embedded and upserted, `mark_embedded` is called, and an attachment with more chunks gets `set_attachment_status(..., embedded_partial=True)`; `update_chain_row`; one OCR job per `needs_ocr` attachment via `enqueue_ocr({"path", "sha256", "ordinal", "pages"})` when set; the file row's `parse_status` is `indexed`, or `indexed_partial` when any attachment ended `unsupported` or `failed`, and the outcome status mirrors it; `ParseTimeout`/`ParseMemoryExceeded`/`ParseError` → `failed` with reason `timeout`/`memory_cap`/the message; logs use `log_subject`); `remove_path(path: str) -> str | None` (unlinks the path; when no live path remains, deletes the message, its chunks and vectors, then `update_chain_row`; returns the removed key); `apply_scan(result: ScanResult) -> list[IngestOutcome]` (emits a `renamed` event per entry of `result.renamed`, since the scan already moved the rows; parses `to_parse`; removes `purged`); `attach_ocr_text(sha256: str, ordinal: int, text: str) -> None` (cache update, re-chunk and re-embed that part, `ocr_status="ok"`).
- conftest: `pipeline(tmp_store, tmp_path, chunker, embedder_or_none)` = `IngestPipeline(tmp_store, ParseCache(tmp_path/"cache"), ParsePool(), chunker, emb, VectorIndex(tmp_store.conn) if emb else None, enqueue_ocr=None)` where `embedder_or_none` uses the real embedder when available else `None`; `pipeline_with_queue` = the same with `enqueue_ocr=lambda p: (jobs.append(p), len(jobs))[1]` over a list `jobs` exposed as `pipeline_with_queue.jobs`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ingest_corpus_indexes_messages_and_chunks(pipeline, corpus_dir):
    outcomes = [pipeline.ingest_file(p, 1) for p in sorted(corpus_dir.glob("*.eml"))]
    assert all(o.status == "indexed" for o in outcomes) and pipeline.store.counts()["chunks"] > len(outcomes)
def test_duplicate_export_indexes_once_and_survives_one_delete(pipeline, corpus_dir):
    src = sorted(corpus_dir.glob("*.eml"))[0]; dup = corpus_dir / "dup.eml"; dup.write_bytes(b"X-Export: 2\r\n" + src.read_bytes())
    k = pipeline.ingest_file(src, 1).message_key; assert pipeline.ingest_file(dup, 1).status == "skipped_duplicate"
    assert len(pipeline.store.paths_for_message(k)) == 2
    pipeline.remove_path(str(dup)); assert pipeline.store.message(k); pipeline.remove_path(str(src)); assert pipeline.store.message(k) is None
def test_attachment_text_is_chunked_as_separate_part(pipeline, corpus_dir):  # the thread-1 last message carries a docx
    ...; assert any(r["part"] == "att:0" for r in pipeline.store.conn.execute("SELECT part FROM chunks WHERE message_key=?", (k,)))
def test_attachment_over_cap_is_keyword_indexed_but_partially_embedded(pipeline, tmp_path, embedder):
    # an .eml whose docx attachment holds LONG_TEXT_30000_WORDS
    ...; rows = store.conn.execute("SELECT embedded FROM chunks WHERE message_key=? AND part='att:0'", (k,)).fetchall()
    assert len(rows) > 64 and sum(r[0] for r in rows) == 64 and store.conn.execute("SELECT embedded_partial FROM attachments WHERE message_key=?", (k,)).fetchone()[0] == 1
def test_image_attachment_enqueues_ocr_job(pipeline_with_queue, corpus_dir):  # thread 3's last message carries the PNG
    ...; assert outcome.ocr_jobs == 1 and pipeline_with_queue.jobs[0]["pages"] == [0]
def test_no_ocr_engine_marks_attachment_text_not_recovered(pipeline, corpus_dir):
    ...; assert store.conn.execute("SELECT ocr_status FROM attachments WHERE message_key=?", (k,)).fetchone()[0] == "unavailable"
def test_attach_ocr_text_makes_text_searchable(pipeline, corpus_dir):
    ...; pipeline.attach_ocr_text(sha, 0, "zebra crossing report"); assert keyword_search(pipeline.store.conn, "zebra", 10, None)
def test_apply_scan_end_to_end(pipeline, corpus_dir):
    r = reconcile(pipeline.store, [corpus_dir], 1, 1); pipeline.apply_scan(r); n = pipeline.store.counts()["messages"]; victim = sorted(corpus_dir.glob("*.eml"))[-1]; victim.unlink()
    pipeline.apply_scan(reconcile(pipeline.store, [corpus_dir], 2, 2)); pipeline.apply_scan(reconcile(pipeline.store, [corpus_dir], 3, 3))
    assert pipeline.store.counts()["messages"] == n - 1 and pipeline.store.conn.execute("SELECT SUM(message_count) FROM chains").fetchone()[0] == n - 1
def test_cache_hit_skips_parse(pipeline, corpus_dir, monkeypatch):
    p = sorted(corpus_dir.glob("*.eml"))[0]; pipeline.ingest_file(p, 1); copy = corpus_dir / "copy.eml"; copy.write_bytes(p.read_bytes())
    monkeypatch.setattr(pipeline.pool, "parse", lambda path: pytest.fail("parsed despite cache hit")); pipeline.ingest_file(copy, 1)
def test_info_log_contains_no_subjects(pipeline, corpus_dir, tmp_path):
    configure_logging("emailsupersearch", tmp_path, "INFO"); [pipeline.ingest_file(p, 1) for p in corpus_dir.glob("*.eml")]
    log = (tmp_path / "logs" / "emailsupersearch.log").read_text(); assert all(subject not in log for subject, _, _ in TOPICS)
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**; wrap each file in try/except recording `failed` with the reason and continuing.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: ingest pipeline from file to chunks, vectors and chains"`

### Task 20: Job queue, OCR engines and OCR worker

**Files:**
- Create: `src/emailsupersearch/jobs/__init__.py`, `src/emailsupersearch/jobs/queue.py`, `src/emailsupersearch/ingest/ocr/__init__.py`, `src/emailsupersearch/ingest/ocr/base.py`, `src/emailsupersearch/ingest/ocr/windows_ocr.py`, `src/emailsupersearch/ingest/ocr/rapid_ocr.py`, `src/emailsupersearch/ingest/ocr/worker.py`
- Modify: `tests/conftest.py`, `src/emailsupersearch/diagnostics/bench.py` (wire `ocr_pages_per_s`)
- Test: `tests/test_jobs.py`, `tests/test_ocr.py`

**Interfaces:**
- Produces in `queue.py`: `@dataclass class Job(id: int, kind: str, payload: dict, state: str, priority: int, attempts: int, last_error: str | None)`; `enqueue(conn, kind: str, payload: dict, priority: int = 100) -> int`; `claim_next(conn, kinds: list[str]) -> Job | None` (lowest priority number first, then id; sets `running`); `complete(conn, job_id)`; `fail(conn, job_id, error: str, retry: bool)` (increments `attempts`; re-queues only when `retry` and the new value is < 3, else `failed`, so the third failure is final); `pending_counts(conn) -> dict[str, int]`.
- Produces in `ocr/base.py`: `class OcrEngine(Protocol): name: str; def available(self, language: str) -> bool; def recognize(self, png: bytes) -> str`; `class OcrUnavailable(Exception)`; `_rapid_importable() -> bool` (tries `import rapidocr`); `select_engine(language: str) -> OcrEngine | None` (Windows OCR when available for the language, else `RapidOcr()` when `_rapid_importable()`, else None).
- `windows_ocr.py`: `WindowsOcr` via `winrt.windows.media.ocr.OcrEngine.try_create_from_language(Language(tag))`, PNG → `SoftwareBitmap` through `BitmapDecoder` on an in-memory stream, `recognize_async` run with `asyncio.run`, lines joined with `\n`; images downscaled with Pillow so `max(w, h) <= engine.max_image_dimension`.
- `rapid_ocr.py`: `RapidOcr` constructing `rapidocr.RapidOCR(params={"Det.model_path": ..., "Cls.model_path": ..., "Rec.model_path": ...})` with the model files found inside the installed `rapidocr/models` package directory (raise `OcrUnavailable` if absent, never download), texts joined by line.
- `worker.py`: `run_ocr_job(path: str, ordinal: int, pages: list[int], language: str, max_pages: int = 50, time_budget_s: float = 600) -> str` (re-parses the file, takes the attachment bytes, renders PDF pages with `render_pdf_page_png` or decodes the image, OCRs each page until the budget is spent); `ocr_subprocess_main()` for `python -m emailsupersearch.ingest.ocr.worker` reading one JSON job from stdin and writing `{"text": ...}` to stdout; `class OcrRunner: run(job_payload: dict, language: str) -> str` (spawns the subprocess with an 11-minute hard timeout).
- conftest: `conn(tmp_path)` = `connect(tmp_path / "q.sqlite")` after `migrate`.

- [ ] **Step 1: Write the failing tests**

```python
def test_enqueue_claim_complete(conn): id1 = enqueue(conn, "ocr", {"a": 1}, 50); id2 = enqueue(conn, "ocr", {"a": 2}, 10); assert claim_next(conn, ["ocr"]).id == id2; complete(conn, id2); assert claim_next(conn, ["ocr"]).id == id1
def test_fail_retries_three_times_then_fails(conn): j = enqueue(conn, "ocr", {}, 1); [fail(conn, j, "x", retry=True) for _ in range(3)]; assert pending_counts(conn)["failed"] == 1
def test_windows_ocr_reads_rendered_text(): eng = WindowsOcr(); eng.available("en") or pytest.skip("no en OCR language"); assert "hello world" in eng.recognize(write_text_png("hello world")).lower()
def test_rapid_ocr_reads_rendered_text(): pytest.importorskip("rapidocr"); assert "hello world" in RapidOcr().recognize(write_text_png("hello world")).lower()
def test_select_engine_returns_none_when_nothing_available(monkeypatch): monkeypatch.setattr(WindowsOcr, "available", lambda self, l: False); monkeypatch.setattr(base, "_rapid_importable", lambda: False); assert select_engine("en") is None
def test_run_ocr_job_on_image_attachment(tmp_path):
    p = tmp_path / "img.eml"; p.write_bytes(make_eml_with_png_bytes("zebra crossing")); select_engine("en") or pytest.skip(); assert "zebra" in run_ocr_job(str(p), 0, [0], "en").lower()
def test_ocr_runner_subprocess_roundtrip(tmp_path): ...same via OcrRunner().run({"path": str(p), "ordinal": 0, "pages": [0]}, "en")
```
(`make_eml_with_png_bytes(text)` is added to `tests/helpers.py` in this task.)

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**; the OCR worker process imports engines lazily and never both in one process unless the first is unavailable.
- [ ] **Step 4: Run to verify pass** → PASS (skips where engines are absent)
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: durable job queue, Windows and rapidocr engines, isolated OCR worker"`

### Task 21: Supervisor (timers, watcher, dispatch, re-index)

**Files:**
- Create: `src/emailsupersearch/jobs/supervisor.py`, `src/emailsupersearch/ingest/watcher.py`
- Modify: `src/emailsupersearch/cli.py`, `tests/conftest.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Produces in `watcher.py`: `class FolderWatcher: __init__(roots: list[Path], on_change: Callable[[set[Path]], None], debounce_s: float = 2.0, force_polling: bool = False)`; `start()` (thread running `watchfiles.watch(*roots, debounce=int(debounce_s*1000), force_polling=force_polling, stop_event=self._stop)` calling `on_change` with the set of changed parent directories); `stop()` (sets the event, joins with a 5 s timeout); `is_network_path(p: Path) -> bool` (UNC, or a drive whose `GetDriveTypeW` is `DRIVE_REMOTE`).
- Produces in `supervisor.py`: `@dataclass class SyncStatus(project_id: str, state: Literal["idle","scanning","parsing","ocr","reindexing"], last_scan_at: int | None, files_total: int, files_pending: int, ocr_pending: int, metadata_only: int, last_error: str | None)`; `class ProjectSupervisor: __init__(project_id: str, store: ProjectStore, pipeline: IngestPipeline, ocr_runner: OcrRunner | None, language: str = "en", reconcile_interval_s: int = 900, on_event: Callable[[dict], None] | None = None)`; `start()` (if setting `reindex_required` is true: clear `chunks` and `chunks_vec`, reset every file's `parse_status` to `pending` so the next reconcile re-parses every file from the cache through Task 18's pending rule, clear the flag; then an immediate reconcile, then the timer; watcher on local roots, polling on network roots; OCR dispatch loop draining `claim_next(["ocr"])` one at a time between scans and applying results via `pipeline.attach_ocr_text`), `stop()` (stops the watcher and timer, then closes the parse pool and the store), `request_rescan(subtree: Path | None = None)`, `status() -> SyncStatus`, `run_once() -> ScanResult` (synchronous reconcile + apply; writes setting `last_synced_at`). Events via `on_event`: `{"type": "scan_started"|"scan_finished"|"file_indexed"|"ocr_done"|"error", ...}`.
- `workers_for_free_ram() -> int` (1 when free RAM < 8 GB, else 2); `build_project_supervisor(project_id: str, store: ProjectStore, embedder: OnnxEmbedder | None, chunker: Chunker, cache_dir: Path, on_event: Callable[[dict], None] | None) -> ProjectSupervisor` (reads `language`; sets `reindex_required` when `store.get_setting("embedder_id")` differs from `embedder.id if embedder else None` or `store.get_setting("chunker_version") != CHUNKER_VERSION`, and then records the new values; wires `ParseCache(cache_dir)`, `ParsePool(workers=workers_for_free_ram())`, `VectorIndex(store.conn)` when an embedder exists, `IngestPipeline` with `enqueue_ocr=lambda payload: enqueue(store.conn, "ocr", payload)` only when `select_engine(language)` is not None (else `None`), `OcrRunner()` in the same condition, and `reconcile_interval_s` from the store setting).
- CLI `sync --project <id> [--once]` (exits 2 with the reason when `validate_data_root(data_root())` refuses).
- conftest: `make_supervisor(tmp_store, corpus_dir, interval_s, with_ocr)` via `build_project_supervisor` with `reconcile_interval_s` overridden; fixtures `sup` (900 s), `sup_fast` (1 s), `sup_slow` (3600 s), `sup_with_ocr` (skips when `select_engine("en")` is None).

- [ ] **Step 1: Write the failing tests**

```python
def test_run_once_indexes_corpus(sup, corpus_dir): r = sup.run_once(); assert r.to_parse and sup.store.counts()["messages"] > 0 and sup.store.get_setting("last_synced_at")
def test_timer_rescans(sup_fast, corpus_dir):
    sup_fast.start(); (corpus_dir / "new.eml").write_bytes(make_eml_bytes()); wait_until(lambda: sup_fast.store.get_file(str(corpus_dir / "new.eml")), 10); sup_fast.stop()
def test_watcher_triggers_rescan(sup_slow, corpus_dir):
    sup_slow.start(); wait_until(lambda: sup_slow.status().state == "idle", 30); (corpus_dir / "w.eml").write_bytes(make_eml_bytes("w")); wait_until(lambda: sup_slow.store.get_file(str(corpus_dir / "w.eml")), 15); sup_slow.stop()
def test_ocr_jobs_are_drained(sup_with_ocr, corpus_dir): sup_with_ocr.start(); wait_until(lambda: pending_counts(sup_with_ocr.store.conn).get("queued", 0) == 0 and sup_with_ocr.status().state == "idle", 120); sup_with_ocr.stop()
def test_status_reports_metadata_only(sup): sup.run_once(); assert sup.status().metadata_only >= 0 and sup.status().state == "idle"
def test_changed_embedder_id_forces_full_reindex(tmp_store, corpus_dir, chunker, embedder):
    s = build_project_supervisor("p", tmp_store, embedder, chunker, corpus_dir.parent / "cache", None); s.run_once(); n = tmp_store.counts()["chunks"]
    tmp_store.set_setting("embedder_id", "other-model"); s2 = build_project_supervisor("p", tmp_store, embedder, chunker, corpus_dir.parent / "cache", None)
    assert tmp_store.get_setting("reindex_required") is True; s2.start(); wait_until(lambda: s2.status().state == "idle" and tmp_store.counts()["chunks"] == n, 60); s2.stop()
def test_is_network_path(): assert is_network_path(Path(r"\\server\share\x")) and not is_network_path(Path(r"C:\x"))
def test_cli_sync_refuses_onedrive_root(monkeypatch): monkeypatch.setenv("EMAILSUPERSEARCH_DATA_ROOT", r"C:\Users\x\OneDrive\ess"); assert main(["sync", "--project", "p", "--once"]) == 2
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** with one worker thread per supervisor (a `queue.Queue` of commands `scan`, `rescan(subtree)`, `stop`), the timer as a `threading.Timer` re-armed after each scan.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: project supervisor with reconcile timer, watcher trigger, OCR dispatch and re-index"`

### Task 22: Filters to candidates

**Files:**
- Create: `src/emailsupersearch/search/__init__.py`, `src/emailsupersearch/search/filters.py`
- Test: `tests/test_filters.py`

**Interfaces:**
- Produces: `LIVE_MESSAGE_SQL = "EXISTS (SELECT 1 FROM files f WHERE f.message_key = m.message_key AND f.tombstoned_at IS NULL)"`; `candidate_message_keys(conn, f: SearchFilters) -> list[str] | None` (`None` when no filter is set and the project has no tombstoned file rows; otherwise SQL over `messages m` always including `LIVE_MESSAGE_SQL`: `from_addrs` → `sender_addr IN` (normalised), `recipient_addrs` → `EXISTS recipients WHERE kind IN ('to','cc') AND addr IN`, `date_from/date_to` → `sent_ts BETWEEN` inclusive, `attachment_kinds` → `EXISTS attachments WHERE kind IN`, `has_attachments` → `has_attachments = 1/0`); `chunk_rowids_for(conn, message_keys: list[str] | None) -> list[int] | None`; `messages_by_date(conn, f: SearchFilters, limit: int, offset: int) -> list[str]` (filter-only searches).

- [ ] **Step 1: Write the failing tests** — `test_no_filters_returns_none`, `test_from_filter_is_case_insensitive`, `test_recipient_filter_matches_to_and_cc_not_bcc`, `test_date_range_inclusive`, `test_attachment_kind_filter`, `test_filters_combine_with_and`, `test_tombstoned_messages_excluded` (tombstone a message's only file row, assert its key is absent), `test_chunk_rowids_for_subset`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: search filters to candidate message keys"`

### Task 23: Hybrid search and result cards

**Files:**
- Create: `src/emailsupersearch/search/hybrid.py`, `src/emailsupersearch/search/results.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_hybrid.py`

**Interfaces:**
- Produces in `results.py`: `@dataclass class AttachmentInfo(ordinal, filename, kind, size, extract_status, ocr_status, embedded_partial: bool, page_count)`; `@dataclass class MessageCard(message_key, subject, sent_ts, sender: Person, to: list[Person], cc: list[Person], snippet: str, attachments: list[AttachmentInfo], paths: list[str], chain_id, score: float)`; `@dataclass class ChainCard(chain_id, subject, first_ts, last_ts, message_count, participants: dict[str, list[Person]] (keys `from`, `to`, `cc`), conclusion: str | None, attachments: list[AttachmentInfo], related: list[tuple[str, str]] (chain_id, subject), messages: list[MessageCard], score: float)`; `@dataclass class SearchResponse(total_messages: int, total_chains: int, page: int, page_size: int, chains: list[ChainCard], messages: list[MessageCard], mode: Literal["hybrid","keyword_only"])`.
- Produces in `hybrid.py`: `RRF_K = 60`; `K_EACH = 50`; `PAGE_SIZE = 20`; `rrf(ranked_lists: list[list[int]], k: int = 60) -> dict[int, float]`; `search(store: ProjectStore, embedder: OnnxEmbedder | None, f: SearchFilters, page: int = 0, page_size: int = PAGE_SIZE) -> SearchResponse`: `keys = candidate_message_keys(store.conn, f)`; `rowids = chunk_rowids_for(store.conn, keys)`; keyword list `keyword_search(store.conn, f.query, K_EACH, rowids)`; vector list `VectorIndex(store.conn).knn(embedder.embed([f.query])[0], K_EACH, rowids)` when `embedder` and `f.query`; fuse with `rrf`; chunk → message by best score (`rank_messages`), snippet = the best chunk's text without its context line, 240 characters; cards only for live messages (`paths_for_message` default); group to chains when `f.group_by_chain` (`group_chains`), chain score = best message; `f.sort == "date"` orders messages by `sent_ts` and chains by `last_ts` descending after fusion; `related` = up to 3 other chains sharing at least 2 participant addresses or an attachment `sha256`; a filter-only query uses `messages_by_date`; `mode` is `keyword_only` without an embedder.
- conftest: `indexed_store` (session scope) builds its own `ProjectStore`, `ParseCache`, `ParsePool`, chunker and `IngestPipeline` under `tmp_path_factory.mktemp("indexed")` with the session `embedder` (it cannot reuse the function-scoped fixtures), ingests `generate(..., 12, seed=5)`, and closes everything at session end; tests that mutate it (the tombstone test) restore the rows afterwards.

- [ ] **Step 1: Write the failing tests**

```python
def test_rrf_scores(): s = rrf([[1,2,3],[3,1]]); assert s[1] > s[2] and abs(rrf([[1]])[1] - 1/61) < 1e-9
def test_identifier_query_finds_invoice_message(indexed_store, embedder): r = search(indexed_store, embedder, SearchFilters(query="INV-2024-00017", group_by_chain=False)); assert "INV-2024-00017" in r.messages[0].snippet
def test_semantic_query_ranks_topic_thread_first(indexed_store, embedder):
    subject, body, _ = TOPICS[4]; r = search(indexed_store, embedder, SearchFilters(query=paraphrase(body))); assert r.chains[0].subject.lower().endswith(subject.lower())
def test_filters_narrow_results(indexed_store, embedder): ...; assert all(c.participants["from"][0].addr == addr for c in r.chains)
def test_group_by_chain_groups_and_pages(indexed_store, embedder): r = search(indexed_store, embedder, SearchFilters(query="renewal"), page_size=2); assert len(r.chains) <= 2 <= r.total_chains
def test_sort_by_date_orders_cards_desc(indexed_store, embedder): r = search(indexed_store, embedder, SearchFilters(query="meeting", sort="date")); ts = [c.last_ts for c in r.chains]; assert ts == sorted(ts, reverse=True)
def test_keyword_only_mode_without_embedder(indexed_store): assert search(indexed_store, None, SearchFilters(query="renewal")).mode == "keyword_only"
def test_filter_only_search_returns_by_date(indexed_store, embedder): r = search(indexed_store, embedder, SearchFilters(query="", date_from=0, group_by_chain=False)); ts = [m.sent_ts for m in r.messages]; assert ts == sorted(ts, reverse=True)
def test_tombstoned_message_hidden_from_search_before_purge(indexed_store, embedder): tombstone the only file row of the invoice message; assert no card has that key; undo
def test_related_chains_share_participants(indexed_store, embedder): assert any(c.related for c in search(indexed_store, embedder, SearchFilters(query="budget")).chains)
```
(`paraphrase(body)` in `tests/helpers.py` returns the body's first sentence with two content words replaced by synonyms from a fixed map.)

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: hybrid search with RRF, chain grouping, sorting and result cards"`

### Task 24: Excel export

**Files:**
- Create: `src/emailsupersearch/export/__init__.py`, `src/emailsupersearch/export/excel.py`
- Test: `tests/test_excel.py`

**Interfaces:**
- Produces: `EXPORT_MAX_ROWS = 5000`; `@dataclass class ExportInfo(path: Path, rows: int, truncated: bool)`; `export_results(messages: list[MessageCard], out_path: Path, max_rows: int = EXPORT_MAX_ROWS) -> ExportInfo`; columns in order: Date (UTC `YYYY-MM-DD HH:MM`), From (`Name <addr>`), To, CC, Subject, Chain, Attachments, Preview, Open (hyperlink with display text `Open`), Path; header row bold and frozen (`freeze_panes="A2"`), auto-filter on the used range, Preview wrapped at width 60, other widths `min(60, max(len)+2)`, Open cell hyperlink `Path(path).as_uri()` with `style="Hyperlink"`.

- [ ] **Step 1: Write the failing tests** — `test_export_writes_expected_columns_and_hyperlink` (reload with openpyxl; header names; `ws["I2"].hyperlink.target.startswith("file:///")`; bold header; frozen panes), `test_export_caps_rows_and_reports_truncation` (6,000 cards → 5,000 rows, `truncated` True), `test_export_percent_encodes_path` (a path with a space and `#` → `%20` and `%23`).
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement** with openpyxl only.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: Excel export with file links"`

### Task 25: Catalog and project lifecycle

**Files:**
- Create: `src/emailsupersearch/catalog.py`
- Test: `tests/test_catalog.py`

**Interfaces:**
- Produces: `@dataclass class Project(id: str, name: str, created_at: int, dir: Path)`; `class Catalog: __init__(root: Path)` (opens `root/catalog.sqlite` with tables `projects(id TEXT PRIMARY KEY, name, created_at)`, `settings(key, value)`, `doctor_runs(ts INTEGER, report_json TEXT)`); `create_project(name: str, folders: list[Path], language: str = "en", embedder_id: str | None = None, embedding_dim: int | None = None) -> Project` (id `uuid4().hex[:12]`; creates the project dir and `exports/`; opens the `ProjectStore`; adds folders with `kind` from `is_network_path`; sets settings `language`, `embedder_id`, `embedding_dim`, `chunker_version=CHUNKER_VERSION`, `reconcile_interval_s=900`, `ocr_enabled=True`, `ocr_engine_order=["windows","rapid"]`, `group_by_chain_default=True`, `export_dir=None`, `saved_searches=[]`); `list_projects()`; `get(project_id)`; `rename(project_id, name)`; `delete_project(project_id)` (removes the directory); `get_setting/set_setting` (global: `port`, `log_level`, `autostart`, `data_root`); `record_doctor_run(report_json: str)`; `open_store(project_id) -> ProjectStore`; `close()`.

- [ ] **Step 1: Write the failing tests** — `test_create_list_get_delete_project`, `test_create_project_initialises_store_settings_and_folders`, `test_saved_searches_roundtrip_in_store_settings`, `test_global_settings_roundtrip`, `test_record_doctor_run`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: project catalog and lifecycle"`

### Task 26: Host security, app factory, project and status routes

**Files:**
- Create: `src/emailsupersearch/host/__init__.py`, `src/emailsupersearch/host/security.py`, `src/emailsupersearch/host/app.py`, `src/emailsupersearch/host/routes_projects.py`, `src/emailsupersearch/host/sse.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_host_security.py`, `tests/test_routes_projects.py`

**Interfaces:**
- Produces in `security.py`: `new_token() -> str` (`secrets.token_urlsafe(32)`); `SESSION_COOKIE = "ess_session"`, `CSRF_COOKIE = "ess_csrf"`, `CSRF_HEADER = "X-ESS-Token"`, `LAUNCH_HEADER = "X-ESS-Launch"`; `LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}`; `class SessionStore: issue() -> tuple[str, str]` (session id, csrf), `csrf_for(session_id) -> str | None`; `class SecurityMiddleware(BaseHTTPMiddleware): __init__(app, launch_token: str, sessions: SessionStore)`: 400 when the Host (without port) is not in `LOOPBACK_HOSTS`; 403 when `Origin` is present and its host is not loopback; `/auth`, `/health` and non-`/api/` paths are exempt from session checks; `/api/*` GET needs a valid `ess_session`; `/api/*` non-GET also needs `X-ESS-Token` equal to the session's csrf; `/api/shutdown` is allowed with `X-ESS-Launch` equal to the launch token instead; responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- Produces in `sse.py`: `class EventBus: attach_loop(loop: asyncio.AbstractEventLoop)` (called from the app's lifespan startup), `publish(topic: str, event: dict)` (thread-safe: when called off the loop it uses `loop.call_soon_threadsafe(queue.put_nowait, event)` for each subscriber; also stores the event as the topic's last), `last(topic: str) -> dict | None`, `subscribe(topic: str) -> AsyncIterator[dict]` (per-subscriber `asyncio.Queue`; keepalive is left to sse-starlette's `ping`).
- Produces in `app.py`: `@dataclass class Services(catalog: Catalog, embedder: OnnxEmbedder | None, chunker: Chunker, supervisors: dict[str, ProjectSupervisor], bus: EventBus, launch_token: str, sessions: SessionStore, start_supervisor: Callable[[str], ProjectSupervisor], static_dir: Path | None, request_shutdown: Callable[[], None], blocked_projects: dict[str, str], purge_requested: bool = False)`; `create_app(services: Services) -> Starlette` (routes from `routes_*` modules; `GET /auth?token=` → when the token matches, issue a session and set `ess_session` (`HttpOnly; SameSite=Strict; Path=/`) and `ess_csrf` (`SameSite=Strict; Path=/`), redirect to `/`; `/health` → `{"ok": true}`; `POST /api/shutdown` → `services.request_shutdown()`; static SPA from `static_dir` with `html=True` when it exists).
- Produces in `routes_projects.py`: `GET /api/status` → `{"version", "degraded": [...], "projects": [{id, name, sync: SyncStatus | null, blocked: str | null}]}`; `GET /api/projects`; `POST /api/projects` body `{name, folders: [str], language}` → 201 Project (starts its supervisor); `GET/PATCH/DELETE /api/projects/{id}` (PATCH `{name?}`; DELETE stops the supervisor, which closes its store, removes it from `services.supervisors`, then deletes the directory); `POST/DELETE /api/projects/{id}/folders` body `{path}`; `GET/PUT /api/projects/{id}/saved-searches`.
- conftest: `host_services(tmp_path, chunker_ws)` = `Services` over `Catalog(tmp_path)`, `embedder=None`, `chunker=chunker_ws`, `start_supervisor` wrapping `build_project_supervisor` with `cache_dir=tmp_path/"cache"`, `launch_token=new_token()`, `request_shutdown=lambda: None`; `token` = that launch token; `raw_client` = `TestClient(create_app(host_services), base_url="http://127.0.0.1")` with no cookies (the default `testserver` Host would be rejected by the middleware); `client_no_header` = `raw_client` after `GET /auth?token=`; `client` = `client_no_header` plus default header `X-ESS-Token` = the `ess_csrf` cookie value; `project` = a project created over `corpus_dir` through `client`.

- [ ] **Step 1: Write the failing tests**

```python
def test_bad_host_rejected(raw_client): assert raw_client.get("/health", headers={"Host": "evil.example"}).status_code == 400
def test_api_requires_session(raw_client): assert raw_client.get("/api/status").status_code == 401
def test_auth_sets_cookies_and_redirects(raw_client, token):
    r = raw_client.get(f"/auth?token={token}", follow_redirects=False); sc = "\n".join(r.headers.get_list("set-cookie"))
    assert r.status_code in (302, 303, 307) and "ess_session=" in sc and "HttpOnly" in sc and "ess_csrf=" in sc and "SameSite=Strict" in sc
def test_wrong_launch_token_rejected(raw_client): assert raw_client.get("/auth?token=nope", follow_redirects=False).status_code == 403
def test_post_requires_csrf_header_too(client_no_header): assert client_no_header.post("/api/projects", json={"name": "x", "folders": [], "language": "en"}).status_code == 401
def test_foreign_origin_rejected(client): assert client.post("/api/projects", json={...}, headers={"Origin": "https://evil.example"}).status_code == 403
def test_shutdown_accepts_launch_header(raw_client, token): assert raw_client.post("/api/shutdown", headers={"X-ESS-Launch": token}).status_code == 204
def test_security_headers_present(client): assert client.get("/health").headers["x-content-type-options"] == "nosniff"
def test_project_crud_and_status(client, corpus_dir): create → 201; GET /api/status lists it with a sync block; PATCH rename; DELETE → 204 and the directory is gone
def test_saved_searches_put_get(client, project): ...
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: session-gated Starlette host with project and status routes"`

### Task 27: Search, message viewer, attachments, open and export routes

**Files:**
- Create: `src/emailsupersearch/host/routes_search.py`, `src/emailsupersearch/host/routes_messages.py`, `src/emailsupersearch/host/routes_export.py`, `src/emailsupersearch/host/sanitize.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_routes_search.py`, `tests/test_sanitize.py`

**Interfaces:**
- Produces in `sanitize.py`: `sanitize_html(html: str) -> str` (stdlib `html.parser`; allowlist `p br div span b i u strong em ul ol li table thead tbody tr td th h1 h2 h3 h4 h5 h6 blockquote pre code a`; `a` keeps only `href` with `http:`/`https:`/`mailto:` and gets `rel="noopener noreferrer" target="_blank"`; drops `script style img iframe object embed link meta form input svg` and the content of script/style; strips every `on*` and `style` attribute).
- Routes: `POST /api/projects/{id}/search` body `SearchFilters` fields plus `{page, page_size}` → `SearchResponse` JSON (dataclasses via `asdict`); `GET /api/projects/{id}/messages/{key}` → `{message: {headers, sender, to, cc, sent_ts, body_html_sanitized, body_text, new_text}, attachments: [AttachmentInfo], paths (live only), chain: {chain_id, messages: [MessageCard]}}`; `GET /api/projects/{id}/messages/{key}/attachments/{ordinal}` → bytes re-extracted from the first live path (`Content-Disposition: attachment`); `POST .../open` → `os.startfile(first live path)` → 204; `POST .../reveal` → `subprocess.Popen(["explorer", "/select,", path])` → 204; `POST /api/projects/{id}/export/excel` body `{filters, pinned_keys?: [str], pinned_chain_ids?: [str]}` → when either pinned list is non-empty export those messages (chains expanded), else the full result list of the filters; writes to the project's `exports/` or the `export_dir` setting → `{path, rows, truncated}`; `GET /api/projects/{id}/exports/{name}` → file download.
- conftest: `indexed_project(client, host_services, corpus_dir)` creates a project over `corpus_dir` through the API (which starts its supervisor), waits with `wait_until(lambda: sup.status().state == "idle" and sup.store.counts()["messages"] > 0, 60)` on `sup = host_services.supervisors[id]`, and returns the id.

- [ ] **Step 1: Write the failing tests**

```python
def test_sanitize_drops_script_and_cid_images_keeps_links():
    out = sanitize_html('<p onclick="x()">Hi <img src="cid:logo"><script>evil()</script><a href="https://a.b" style="c">l</a><a href="javascript:x">j</a></p>')
    assert "script" not in out and "cid:" not in out and 'href="https://a.b"' in out and "onclick" not in out and "javascript" not in out
def test_search_route_returns_cards(client, indexed_project): r = client.post(f"/api/projects/{indexed_project}/search", json={"query": "INV-2024-00017", "group_by_chain": False}); assert r.status_code == 200 and r.json()["messages"]
def test_search_route_accepts_sort(client, indexed_project): assert client.post(..., json={"query": "meeting", "sort": "date"}).status_code == 200
def test_message_route_returns_sanitized_body_and_chain(client, indexed_project): ...; assert "<script" not in body["message"]["body_html_sanitized"] and body["chain"]["messages"]
def test_attachment_download(client, indexed_project): r = client.get(...); assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
def test_open_route_calls_startfile(client, indexed_project, monkeypatch): calls = []; monkeypatch.setattr(os, "startfile", lambda p: calls.append(p), raising=False); assert client.post(...open).status_code == 204 and calls
def test_export_route_writes_xlsx(client, indexed_project): r = client.post(...export/excel, json={"filters": {"query": ""}}); assert r.json()["rows"] > 0 and Path(r.json()["path"]).exists()
def test_export_route_uses_pinned_keys_when_present(client, indexed_project): r = client.post(...export/excel, json={"filters": {"query": ""}, "pinned_keys": [one_key]}); assert r.json()["rows"] == 1
```

- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: search, message viewer, attachment, open and export routes"`

### Task 28: Sync routes, settings routes, progress stream and folder picker

**Files:**
- Create: `src/emailsupersearch/host/routes_sync.py`, `src/emailsupersearch/host/routes_settings.py`, `src/emailsupersearch/host/folder_picker.py`
- Test: `tests/test_routes_sync.py`, `tests/test_routes_settings.py`

**Interfaces:**
- Sync routes: `POST /api/projects/{id}/sync` → 202 and `supervisor.request_rescan()`; `GET /api/projects/{id}/sync/status` → `SyncStatus`; `GET /api/projects/{id}/sync/events` → `text/event-stream` (sse-starlette `EventSourceResponse`: first the current `SyncStatus` as a `status` event, then `bus.subscribe(f"sync:{id}")`); `POST /api/pick-folder` → `{"path": str | None}`.
- Settings routes: `GET /api/settings` → `{data_root, port, log_level, autostart: {"enabled": false, "available": false}, version}`; `PUT /api/settings` body `{port?, log_level?, data_root?}` (a `data_root` change runs `validate_data_root` and returns 400 with the reason when refused; otherwise stores it and reports `restart_required: true`); `GET/PUT /api/projects/{id}/settings` for `reconcile_interval_s`, `ocr_enabled`, `ocr_engine_order`, `export_dir` (validated to exist), `group_by_chain_default`; `POST /api/purge` → sets `services.purge_requested = True`, calls `services.request_shutdown()` and returns 202 (the UI confirms first); the removal itself happens in `serve()` after the server has exited and every store, the catalog and the lock are closed (Task 29).
- `folder_picker.py`: `pick_folder(initial: Path | None = None) -> Path | None` runs `python -c` with `tkinter.filedialog.askdirectory` in a subprocess (10-minute timeout); `None` on cancel.

- [ ] **Step 1: Write the failing tests** — `test_sync_trigger_returns_202_and_status_changes`; `test_sync_events_stream_yields_scan_finished`:
```python
def test_sync_events_stream_yields_scan_finished(client, indexed_project):
    seen = False
    with client.stream("GET", f"/api/projects/{indexed_project}/sync/events") as r:
        client.post(f"/api/projects/{indexed_project}/sync")   # subscribe first, then trigger
        for line in r.iter_lines():
            if '"scan_finished"' in line: seen = True; break
    assert seen
```
`test_pick_folder_route_uses_picker` (monkeypatch `pick_folder`), `test_settings_roundtrip`, `test_settings_refuses_onedrive_data_root` (400 with a reason), `test_project_settings_roundtrip`.
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: sync, settings and folder-picker routes with SSE progress"`

### Task 29: Launcher, single instance, serve and stop

**Files:**
- Create: `src/emailsupersearch/host/lock.py`, `src/emailsupersearch/host/launcher.py`
- Modify: `src/emailsupersearch/cli.py`, `src/emailsupersearch/host/app.py`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Produces in `lock.py`: `@dataclass class InstanceLock(path: Path, pid: int)` with `release()`; `@dataclass class RunningInfo(pid: int, port: int)`; `acquire(root: Path) -> InstanceLock | None` (exclusive create of `root/host.lock` holding the pid; a lock whose pid is not alive is stale and replaced); `read_running(root) -> RunningInfo | None` (from `root/host.json`, None when the pid is dead); `write_running(root, info)`; `clear(root)`; `write_token(root, token)` / `read_token(root)` (`root/host.token`, written then restricted with `icacls <file> /inheritance:r /grant:r "%USERNAME%:F"` through `subprocess.run`, a warning logged if that fails; the value is never logged).
- Produces in `launcher.py`: `DEFAULT_PORT = 8765`; `choose_port(preferred: int) -> int` (bind test on 127.0.0.1; fallback to an OS-assigned port); `redirect_std_streams_to_log(root: Path) -> None` (opens `logs/host-stderr.log`, `os.dup2` onto fds 1 and 2, replaces `sys.stdout`/`sys.stderr`; used when `sys.stdout is None` or `--headless`); `serve(root: Path, port: int | None, headless: bool, open_browser: bool = True) -> int`: exit 2 with the reason when `validate_data_root(root)` refuses; configure logging; if `read_running(root)` is alive → open `http://127.0.0.1:{port}/auth?token={read_token(root)}` and return 0; else acquire the lock, `ensure_layout`, `embedder = load_default_embedder()`, `chunker = Chunker(tokenizer_path(embedding_dir())) if embedder else Chunker(None)`, `catalog.record_doctor_run(report_json(run_doctor(root)))`, build `Services` with `start_supervisor = lambda pid: build_project_supervisor(pid, catalog.open_store(pid), embedder, chunker, root / "cache" / "parsed", lambda ev: bus.publish(f"sync:{pid}", ev))`, start a supervisor for every project (a `SchemaTooNew` from `open_store` is caught, logged, and recorded in `services.blocked_projects[pid]` instead), `request_shutdown = lambda: setattr(server, "should_exit", True)`, run `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=chosen, log_config=None, access_log=False))`, open the browser via `webbrowser.open_new_tab` with the tokenised `/auth` URL, print the URL once when not headless, and on exit stop supervisors (closing their stores), close the catalog, `clear(root)`, release the lock, and remove the data root when `services.purge_requested`; `stop(root: Path) -> int` (POST `/api/shutdown` with header `X-ESS-Launch` = `read_token(root)`; 0 when the process exits within 10 s).
- CLI: `serve [--port N] [--headless] [--no-browser]` (default command), `stop`, `purge --yes` (stops, then removes the data root).

- [ ] **Step 1: Write the failing tests** — `test_choose_port_falls_back_when_busy` (occupy 8765 on 127.0.0.1 in the test), `test_lock_is_exclusive_and_stale_detected`, `test_serve_refuses_onedrive_root` (`serve(Path(r"C:\Users\x\OneDrive\ess"), None, True, False) == 2`), `test_serve_then_stop_roundtrip` (run `serve(root, None, headless=True, open_browser=False)` in a thread; poll `/health`; `stop(root)` → the thread exits within 10 s), `test_second_serve_opens_existing_instance` (monkeypatch `webbrowser.open_new_tab`; assert it is called with the running port and `/auth?token=`), `test_auth_url_not_in_logs` (after the roundtrip, no log file under `root/logs` contains `/auth?token=`), `test_redirect_std_streams_writes_to_log` (in a subprocess with `sys.stdout = None` before the call).
- [ ] **Step 2: Run to verify failure** → FAIL
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run to verify pass** → PASS
- [ ] **Step 5: Commit** — `git add src tests && git commit -m "feat: host launcher with single instance, port fallback, serve and stop"`

### Task 30: UI scaffold, API client and state

**Files:**
- Create: `ui/package.json`, `ui/vite.config.ts`, `ui/tsconfig.json`, `ui/index.html`, `ui/src/main.tsx`, `ui/src/api.ts`, `ui/src/state.ts`, `ui/src/types.ts`, `ui/src/api.test.ts`, `ui/src/state.test.ts`, `scripts/build_ui.sh`

**Interfaces:**
- `ui/src/types.ts` mirrors `SearchFilters`, `SearchResponse`, `ChainCard`, `MessageCard`, `Person`, `AttachmentInfo`, `SyncStatus`, `Project`, and `Status` (the `GET /api/status` body) exactly as the Python side serialises (snake_case keys).
- `api.ts`: `class Api { status(); settings(); putSettings(body); projects(); createProject(body); deleteProject(id); projectSettings(id); putProjectSettings(id, body); search(projectId, filters, page); message(projectId, key); attachmentUrl(projectId, key, ordinal); openInOutlook(projectId, key); reveal(projectId, key); exportExcel(projectId, filters, pinnedKeys, pinnedChainIds); syncNow(projectId); syncEvents(projectId): EventSource; pickFolder(); savedSearches(projectId); putSavedSearches(projectId, list); purge() }`; every non-GET sends header `X-ESS-Token` read from the `ess_csrf` cookie.
- `state.ts`: `type State = { projectId: string | null; filters: SearchFilters; page: number; results: SearchResponse | null; selectedKey: string | null; pinnedKeys: Set<string>; pinnedChainIds: Set<string>; tab: "results" | "chat" | "graph" | "artefacts"; status: Status | null; busy: boolean; error: string | null }`; `reducer(state, action)` with actions `setProject`, `setFilters`, `setPage`, `setResults`, `select`, `togglePinKey`, `togglePinChain`, `clearPins`, `setTab`, `setStatus`, `setBusy`, `setError`; `filtersToBody(filters, page): object` (omits empty lists and nulls).
- Vite builds to `../src/emailsupersearch/ui/dist` with `base: "/"`; no external URLs; system font stack. `scripts/build_ui.sh`: `cd ui && npm ci && npm run build`.

- [ ] **Step 1: Write the failing vitest tests** — `state.test.ts`: `setFilters resets page to 0`, `setProject clears results, selection and pins`, `togglePinKey adds then removes`; `api.test.ts`: `filtersToBody omits empty lists and nulls`, `non-GET requests include X-ESS-Token from the ess_csrf cookie` (mock `fetch` and `document.cookie`).
- [ ] **Step 2: Run** `cd ui && npm install && npx vitest run` → FAIL
- [ ] **Step 3: Scaffold** (`npm create vite@latest` with the `preact-ts` template, versions pinned in `package.json`) and implement `api.ts`, `state.ts`, `types.ts`.
- [ ] **Step 4: Run to verify pass** — `npx vitest run` → PASS; `npm run build` → files under `src/emailsupersearch/ui/dist/`; `.venv/Scripts/python -m build --wheel` and `unzip -l dist/*.whl | grep ui/dist/index.html` → present.
- [ ] **Step 5: Commit** — `git add ui scripts/build_ui.sh && git commit -m "feat: UI scaffold with typed API client and state reducer"`

### Task 31: UI screens for Layout A

**Files:**
- Create: `ui/src/App.tsx`, `ui/src/components/LeftPane.tsx`, `ProjectSwitcher.tsx`, `SearchBox.tsx`, `Filters.tsx`, `SavedSearches.tsx`, `StatusLine.tsx`, `Tabs.tsx`, `ResultsTab.tsx`, `ChainCard.tsx`, `MessageCard.tsx`, `MessageViewer.tsx`, `ProjectDialog.tsx`, `SettingsDialog.tsx`, `PlaceholderTab.tsx`, `ui/src/styles.css`
- Test: `ui/src/components/Filters.test.tsx`, `ui/src/components/ResultsTab.test.tsx`; manual browser verification below

**Interfaces:**
- Renders layout A from the mockup: left pane (project switcher with folder count, message count and last sync; search box submitting on Enter; From chips and To/CC chips with free-text add; two date inputs; attachment-kind chips `PDF Excel Word PowerPoint Images`; "Group by chain" switch; "Only chains with a conclusion" switch disabled with tooltip "Needs the chat milestone"; saved searches list with Save current; status line with degraded badges, `N metadata-only` with a tooltip, and Sync now; New project and Settings buttons). Main area tabs `Results | Chat | Graph | Artefacts`; Chat, Graph and Artefacts render `PlaceholderTab` ("Coming in the next milestone"). Results header: `N chains · M emails match`, a Relevance/Date sort toggle, `N pinned` with Clear, Export to Excel (sends pins when any). Chain card: checkbox, subject, count and span, From/To/CC names with addresses on hover, conclusion block only when present, attachments with badges `text recovered` / `OCR pending` / `text not recovered` (`ocr_status` `unavailable`) / `partially embedded` (`extract_status` `partial_embed`) / `not indexed` (`unsupported`, `failed`), related chains, actions Open in Outlook, Add to chat (disabled), Show in graph (disabled), All N emails expanding `MessageCard`s (each with its own checkbox). `MessageViewer` as a right-side drawer: headers, sanitised HTML in `<iframe sandbox="" srcdoc=…>`, attachment list with download links, chain list. `ProjectDialog`: name, folders (typed path or Browse via `/api/pick-folder`), OCR toggle; used by the first-run flow when there are no projects. `SettingsDialog`: global port and log level, data root (shows the refusal reason on 400), per-project reconcile interval, OCR enabled and engine order, export folder, group-by-chain default; Stop host and Purge (with confirmation); autostart row disabled with "Coming in the hardening milestone".

- [ ] **Step 1: Write the failing component tests** — `Filters.test.tsx`: typing an address and pressing Enter adds a From chip and dispatches `setFilters` with the lower-cased address; toggling a kind chip adds and removes it. `ResultsTab.test.tsx`: the sort toggle in the Results header dispatches `setFilters` with `sort: "date"`; ticking a chain card's checkbox dispatches `togglePinChain`.
- [ ] **Step 2: Run** `npx vitest run` → FAIL
- [ ] **Step 3: Implement** the components and styles (CSS variables with `prefers-color-scheme`, no external fonts).
- [ ] **Step 4: Run to verify pass** — `npx vitest run` → PASS; `npm run build`; `du -sb ../src/emailsupersearch/ui/dist` under 5 MB.
- [ ] **Step 5: Verify in the browser** — `EMAILSUPERSEARCH_DATA_ROOT=$TEMP/ess-root .venv/Scripts/python -m emailsupersearch serve --port 8765`; corpus: `.venv/Scripts/python -m emailsupersearch.diagnostics.corpus $TEMP/ess-corpus 50 1`; open the printed URL; create a project over that folder. Checkable results: the status line reaches `idle` with 50 threads' files counted; searching `INV-2024-00017` shows the invoice message first; a From chip narrows results; the Date sort reorders cards; pinning two cards and exporting yields a 2-row workbook whose Open links resolve; clicking a card opens the viewer with a sanitised body; Settings rejects a OneDrive data root with a reason; the Chat tab shows the placeholder.
- [ ] **Step 6: Commit** — `git add ui && git commit -m "feat: layout A UI with results, filters, pins, viewer, project and settings dialogs"`

### Task 32: CI, lock files, install documentation and 0.1.0

**Files:**
- Create: `.github/workflows/ci.yml`, `docs/install.md`, `requirements-lock-3.12.txt`, `requirements-lock-3.13.txt`
- Modify: `README.md`, `pyproject.toml` (version `0.1.0`), `src/emailsupersearch/__init__.py`
- Test: `tests/test_zero_network_guard.py`

**Interfaces:**
- `ci.yml`: on push and pull request; `windows-latest`; `shell: bash`; matrix Python `3.12`, `3.13`; steps: checkout; setup-python; setup-node 22; `bash scripts/build_ui.sh`; `python scripts/fetch_models.py` (cache key = the pinned hashes); `python -m pip install -e ".[dev,ocr]" -e packages/models-en --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console`; `python -m pytest -q`; `python -m build --wheel -o dist .` and `python -m build --wheel -o dist packages/models-en`; the resolution check `python -m pip install --dry-run --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console --find-links dist --report "$RUNNER_TEMP/report.json" "emailsupersearch[all]"` followed by a one-line Python assertion that no entry in the report's `install` list has a URL ending in `.tar.gz` other than the two named packages; `du -sb src/emailsupersearch/ui/dist` must be under 5,242,880 bytes; `pip freeze --exclude-editable > requirements-lock-<version>.txt` uploaded as an artifact and committed on tags.
- `tests/test_zero_network_guard.py`: a session fixture monkeypatching `socket.socket.connect` to record and raise on any address other than `127.0.0.1`/`::1`, then: generate corpus → create project → `run_once` → search → export → when `select_engine("en")` is available, drain one OCR job through `OcrRunner`; asserts no blocked connection was attempted.
- `docs/install.md`: the three-line install (spec 7.3) with the `py.exe` launcher note and the Global Constraints install line; `python -m emailsupersearch`; the offline bundle (`pip download -r requirements-lock-3.12.txt --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console --platform win_amd64 --platform any --python-version 3.12 --abi cp312 --abi abi3 --abi none -d bundle` then `pip install --no-index --find-links bundle emailsupersearch[all]`); where data lives; stop, upgrade (`stop`, `pip install --upgrade`, `serve`) and `purge`; a paragraph that Office may show a "hyperlinks can be harmful" prompt on `file://` links, that only the user (an HKCU setting) or IT can suppress it, and that the in-app Open in Outlook button is the primary path.

- [ ] **Step 1: Write the guard test** and run it → PASS (it fails only if a dependency phones home; record the result either way).
- [ ] **Step 2: Implement** `ci.yml`, `docs/install.md`, the README quick start, the lock files from the local venvs; bump to `0.1.0`.
- [ ] **Step 3: Run the whole suite** — `.venv/Scripts/python -m pytest -q` → PASS; build both wheels; install them into a fresh `py -3.12 -m venv` with the install line and `--find-links dist`; run `doctor` there → OK.
- [ ] **Step 4: Commit and tag** — `git add -A && git commit -m "chore: CI, lock files, install docs, zero-network guard; v0.1.0" && git tag v0.1.0`

---

## Self-review notes

- Spec coverage for this plan's scope: 6.1 topology (Tasks 19 to 21, 29), 6.2 layout (1, 25), 7.1 to 7.3 (1, 3, 32), 7.4 doctor (5), 7.6 stop/purge/lock/schema-too-new (4, 29), 8.1 re-index on model change (21), 8 data model (4, 15), 9.1 to 9.6 (7 to 12, 17 to 19), 10.1 to 10.2, 10.4 to 10.6 (13, 16, 22 to 24, 27, 31), 14.1 to 14.3 (26 to 31), 14.2 settings (28, 31), 15 headless redirection (29; the autostart shortcut is M4), 17 zero-network and log redaction (1, 2, 19, 32), 18 doctor and bench (5, 14), 20 criteria 1 to 5 and 9 (32, 19, 23, 20, 5). Chain conclusions (10.3), chat (11), artefacts (12), graph (13) and autostart are later milestones by design.
- Names used across tasks are defined once: `ParsedMessage`, `ParsedDocument`, `AttachmentText`, `Person`, `SearchFilters`, `ProjectStore`, `ParseCache`, `ParsePool`, `Chunker`, `OnnxEmbedder`, `VectorIndex`, `keyword_search`, `assign_chain`, `update_chain_row`, `reconcile`/`ScanResult`, `IngestPipeline`, `OcrRunner`, `select_engine`, `ProjectSupervisor`/`SyncStatus`/`build_project_supervisor`, `search`/`SearchResponse`, `export_results`, `Catalog`, `Services`, `SessionStore`, `EventBus`, `InstanceLock`.
- Reviewed on 2026-10-24 by three independent plan reviewers (coverage, interfaces, executability); their 60 findings are folded in above, including the two blockers: the `--no-binary` exception for extract-msg's source-only dependencies, and the explicit `py -3.12` venv. A second pass verified 58 of 60 resolved and found 24 issues introduced by the rewrite (shared SQLite connection across threads, the re-index path that never re-parsed, three tests that could not pass as written, Windows file locks on delete and purge); all are folded in.
