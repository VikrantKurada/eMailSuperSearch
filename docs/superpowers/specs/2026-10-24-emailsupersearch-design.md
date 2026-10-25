# eMailSuperSearch: design

Date: 2026-10-24. Status: draft for owner approval.
Sources: `eMailSuperSearch_PRD.txt` (PRD, status "Final"), the owner's decisions in conversation on 2026-10-23/24, and the verified research in `docs/research/` (feasibility sweep and risk register). Where this document departs from the PRD, the risk register says why.

Reference material the builder receives with this document: the research folder `docs/research/` (the evidence behind every Call below), and the two mockup pages the owner reviewed, `docs/superpowers/specs/mockups/2026-10-24-layout.html` and `docs/superpowers/specs/mockups/2026-10-24-artefacts.html` (open in any browser). The mockups are binding for the layout and for the content listed in sections 10.2, 12.1 and 14.1; their visual styling is guidance, not a contract.

Every statement in this document is one of three things: **Owner** (decided by the owner, in their words where possible), **Call** (decided by the author on the owner's behalf, grounded in the research, and open to correction), or **Builder** (left to whoever implements it).

---

## 1. Intent and the why

A Python library, installed with `pip`, that turns a folder of archived Outlook mail on a locked-down corporate laptop into something you can search by meaning, ask questions of, and make documents from, without a single byte leaving the machine.

The owner archives their mail as `.msg` files. Finding something later means remembering a subject line or a sender. What they actually remember is who was involved, roughly what it was about, what was attached, and how a chain ended. They want to search on those things, chat with the results, and turn a set of results into a timeline, a diagram, a brief, or a table of decisions they can paste into their own work.

The PRD describes the engine for this as a developer's own scripts. The owner wants it to be a product: one `pip install` on an office laptop where they have no admin rights and cannot install software, a web page that opens in the browser, and nothing phoning home. Everything else in this document follows from those two facts.

## 2. Who it is for and the moment it serves

**Owner:** the owner is the first and, for v1, the only user, searching their own archived mail on their office laptop. Colleagues are not a v1 audience.

The moment: sitting at the laptop, needing to answer "what did we agree with X and who signed off", "which emails had that spreadsheet attached", "who was copied on the chain about Y", or "give me a one-page summary of how Z unfolded" and getting it in seconds, with each claim pointing at the email it came from.

### 2.1 Facts about the owner's environment (Owner, 2026-10-24)

- The archived mail folders live on the laptop's local disk only. Nothing syncs them.
- The archive holds between 50k and 100k emails, at the top of the v1 design target. The owner did not flag scanned PDFs or protected mail as unusually common.
- The laptop already has a per-user python.org Python, and `pip` has installed compiled packages there, so executables and native wheels run from the user profile.
- From the laptop, `pip` reaches PyPI. Hugging Face is blocked.
- Development happens on the owner's own desktop, which is not the laptop and has no Outlook.

## 3. Goals, non-goals, anti-goals

### Goals (v1)

1. Mirror one or more folders of `.msg` (and `.eml`) files into a searchable index, keeping it current in the background and pruning what the user deletes.
2. Search by meaning and by exact words, filtered by sender, recipients (To and CC), date range, attachment type, and grouped by email chain.
3. Make attachment content searchable: PDF, Word, Excel, PowerPoint text, and OCR of scanned pages and images.
4. Show, for a chain, who was involved, what was attached, what the chain concluded, and which other chains relate to it.
5. Chat with the current results on a local LLM, with every answer citing sender and date, and refusing when the answer is not in the results.
6. Generate four kinds of artefact from a set of results: a chain timeline, a who-talked-to-whom map, a written brief, and a decisions-and-open-items table.
7. Export search results to Excel with working links back to the original files.
8. Install and run as a standard Windows user with `pip` alone; work fully offline after installation and a one-time model download.

### Non-goals (v1)

- Multiple users, shared indexes, or running on a server.
- Reading live mailboxes, PST or OST files. Only archived files on disk.
- Sending, replying to, or editing email.
- Any operating system other than Windows 11 x64.
- Any cloud or remote LLM.
- A mobile layout.
- A public, documented Python API. The v1 surfaces are the web UI and the command line. The internal service layer is kept separate from the web handlers so an API can be exposed later (7.7).

### Anti-goals (a build that does these has failed even if it works)

- Any outbound network call during normal operation, including telemetry, update checks, model downloads, or CDN assets.
- Needing administrator rights at any step, including installation, first run, autostart, and upgrade.
- Writing the index, models, logs or caches anywhere outside the user's local profile, or into roaming or OneDrive-synced folders. Exports and artefacts are the exception: the user may send those wherever they like (10.4).
- Leaking message content through logs or diagnostic reports.
- Losing or corrupting an index because of a `pip` upgrade, a VPN drop, or a share going offline.
- Making the laptop unusable while ingestion runs.

## 4. Constraints

| ID | Constraint | Source |
|----|-----------|--------|
| C1 | Delivered as a Python library on PyPI. The only thing the user installs is this library via `pip`. No Node, Electron, MSI, or separate installers. | Owner |
| C2 | Modular install: the user can leave out the LLM engine, the LLM weights, OCR, or the NLP graph models. What remains keeps working. | Owner |
| C3 | Web UI served locally by the library, works offline, opens in the default browser. Projects for searches; search pane on the left. | Owner |
| C4 | Corporate laptop, standard user, no admin, cannot install software. | Owner |
| C5 | Background sync keeps each project a 1:1 mirror of its folders. | PRD 4.1, Owner |
| C6 | Hardware: Ryzen 5 PRO 8640HS, Radeon 760M iGPU, 16 GB shared RAM. LLM under 6 GB resident. Graph views capped at about 500 nodes. | PRD 2, 5.1 |
| C7 | Zero telemetry; all data, models, logs, and dependencies under the user profile. | PRD 5.2 |
| C8 | Licence GPL-3.0-or-later for the library. | Owner |

## 5. Decisions made by the owner

Quoted or closely paraphrased from the conversation.

- Delivery: "a python library that I plan to put on PIP"; "installation is modular. The user can opt out of installing individual components such as GPT4ALL, the LLM model etc."
- UI: "Web based user interface that runs offline and does not require any installation apart from the python library. User can create projects for searches and search pane exists in the left of the screen."
- A project "is its own set of mail folders with its own index, knowledge graph, chat history and exports."
- First user: "I'm the first user, searching my own archived mail on my office laptop."
- Capabilities: search "whom was the email sent to, who were in CC, what was the rough summary of the email, what attachments did the email have, what were the conclusions of the email chain, details of how the contents of the email relates to the other emails", and "generate content such as svg based images, write up content, other possible kinds of artefacts and do chat with the search results."
- Layout: Layout A from the mockups: search pane left, one main area with tabs Results, Chat, Graph, Artefacts. The owner raised no objection to the result card, chat, and artefact content shown in the mockup.
- Artefacts for v1: "All four: chain timeline, who-talked-to-whom map, written brief, and decisions table."
- Licence: "GPL-3 for the library is fine; keep extract-msg as the parser."

The owner was shown, and did not object to, the technical defaults in sections 7, 10, 13, 15 and 16 (default model, deterministic graph with opt-in LLM enrichment, weights by download or sideload, Startup-folder host process, 100k emails per project, Python 3.12 to 3.13, English-only base models). They are recorded below as Calls.

## 6. Architecture overview

### 6.1 Process topology (Call)

One long-lived **host process** owns everything: the web server, the single SQLite writer, the job queue, and the file watcher. Short-lived **worker processes** do the dangerous or heavy work and return results to the host.

```
python -m emailsupersearch
  └─ host process (uvicorn on 127.0.0.1, single-instance lock)
       ├─ web server: static SPA + JSON/SSE API (token-gated)
       ├─ sync supervisor: watchfiles events + periodic reconcile scans
       ├─ job queue (SQLite table) and scheduler
       ├─ SQLite writer (one connection per project DB, WAL)
       ├─ embedder (in-process ONNX session, thread pool)
       ├─ graph builder (in-process)
       ├─ parse workers      (subprocess pool, 1-2; extract-msg, attachments; per-file timeout)
       ├─ OCR worker          (one subprocess; Windows OCR, rapidocr fallback; isolated on purpose)
       └─ LLM worker          (one subprocess; llama.cpp; lazy-loaded, idle-unloaded; serves chat OR enrichment, never both)
```

Why: the research found that the Windows OCR runtime and rapidocr crash when loaded in one process; that llama.cpp, OCR and embedding all compete for the same 16 GB and the same iGPU; and that a 1:1 mirror needs one writer and a durable queue. The PRD's separate Task Scheduler job is replaced by this host process (section 15).

### 6.2 Data layout on disk (Call)

All under `%LOCALAPPDATA%\eMailSuperSearch\` (non-roaming, not protected by Controlled Folder Access, not under OneDrive):

```
catalog.sqlite                 list of projects, global settings, doctor results
projects\<project-id>\
    project.sqlite             everything the project owns (section 8)
    exports\                   artefacts and Excel exports the project produced
cache\parsed\<sha256>.json     parsed text + OCR output per source file, shared across projects
models\                        LLM weights (.gguf) and their checksums
logs\                          rotating logs, one file set per process
venv\                          recommended install location (section 7.3)
```

The parse cache is shared so a file watched by two projects is parsed and OCR'd once. Each project still embeds, indexes and builds its graph independently.

A custom data root is allowed in settings; the UI refuses paths containing `\OneDrive\` or under the Windows known folders (Documents, Desktop) and says why.

## 7. Packaging and distribution

### 7.1 Distributions on PyPI (Call)

| Distribution | Contents | Tag | Size (approx) |
|--------------|----------|-----|---------------|
| `emailsupersearch` | all Python code, the compiled web UI, SVG/Word/Excel templates | `py3-none-any` | 3 to 6 MB |
| `emailsupersearch-models-en` | int8 ONNX all-MiniLM-L6-v2 + tokenizer (Apache-2.0), spaCy `en_core_web_sm` data (MIT) | `py3-none-any` | ~35 MB |
| `emailsupersearch-engine` | llama.cpp shared libraries in two builds, `vulkan\` and `cpu\`; the Python binding is a vendored copy of llama-cpp-python (MIT, ctypes-based, pinned to the exact llama.cpp build) imported under the library's own namespace so it never collides with a user-installed `llama_cpp`; a small loader that picks the DLL directory; Microsoft C runtime DLLs app-local | `py3-none-win_amd64` | ~60 MB |

All three stay under PyPI's 100 MiB per-file limit. Request a project size increase early, because every engine release costs ~60 MB of the 10 GiB project quota.

Why separate wheels: the core must stay pure Python so `pip` never tries to compile; the engine is Windows-specific native code; the models package lets a user skip 35 MB they do not want, and lets the spaCy model (hosted by spaCy on GitHub, not PyPI) be a real PyPI dependency.

### 7.2 Extras (Call)

```
pip install emailsupersearch              core: parse, index, search, filters, Excel export, header graph, web UI
pip install emailsupersearch[nlp]         + emailsupersearch-models-en (semantic search needs it; without it search is keyword-only)
pip install emailsupersearch[ocr]         + rapidocr, opencv-python-headless (fallback OCR engine, ~110 MB)
pip install emailsupersearch[llm]         + emailsupersearch-engine, huggingface_hub (chat, conclusions, brief, decisions table, enrichment)
pip install emailsupersearch[all]         everything above
```

Degradation when an extra is absent, shown as a badge in the UI, never as an error:

| Missing | Still works | Disabled |
|---------|-------------|----------|
| `[nlp]` | keyword search, filters, chains, header graph, timeline and people-map artefacts, Excel export | semantic search, entity nodes |
| `[ocr]` and no Windows OCR language | everything else; scanned pages indexed by filename only, flagged "text not recovered" | OCR |
| `[llm]` or no weights downloaded | search, graph, timeline, people map, Excel | chat, chain conclusions, written brief, decisions table, LLM enrichment |

The core always depends on the Windows OCR bridge packages (`winrt-Windows.Media.Ocr` and its five companions, about 1 MB, marker `sys_platform == 'win32'`), so OCR works out of the box whenever Windows has the language installed.

### 7.3 Supported Python and install recipe (Call)

Requires-Python `>=3.12,<3.14`. These are the only versions on which every dependency ships a Windows wheel, including the `msvc-runtime` remedy below.

Documented install for a standard user:

1. Install Python per-user from python.org ("Install Now", with "use admin privileges when installing py.exe" unticked) or with the Python install manager. Both need no admin.
2. `py -m venv %LOCALAPPDATA%\eMailSuperSearch\venv` (short path, non-roaming; `pip --user` would land in the roaming profile and Store Python's deep path breaks long-path limits).
3. `%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all]"` (the two named packages are pure-Python dependencies of extract-msg that exist only as source archives; they build without a compiler. Verified during planning on 2026-10-24.)
4. `...\python -m emailsupersearch` opens the UI. Launch is always `python -m`; console scripts are not on PATH for user installs and freshly built `.exe` launchers are blocked by Defender's prevalence rule.

Offline install: `pip download` on a connected machine with `--only-binary=:all: --platform win_amd64 --platform any --python-version 3.12 --abi cp312 --abi abi3 --abi none`, then `pip install --no-index --find-links`. The library publishes a pinned lock file per release so the bundle resolves identically. The owner's laptop reaches PyPI, so the online install above is the primary path and the bundle is the fallback.

### 7.4 Native code, the C runtime, and application control (Call)

- Every native dependency (onnxruntime, llama.cpp, sqlite-vec, opencv) loads `msvcp140.dll` and friends, which Windows does not ship and a standard user cannot install system-wide. The engine wheel carries these DLLs app-locally and registers its directory with `os.add_dll_directory` before any import. The `doctor` command checks for them first and, if the core's own native modules fail for this reason, suggests `pip install msvc-runtime` into the base interpreter.
- The engine wheel's `vulkan\` build is tried first; if `vulkan-1.dll` is missing or allocation fails, the loader retries with host-memory preference, then partial offload, then the `cpu\` build. The chosen mode is recorded and shown in Settings. The user never picks a GPU extra.
- The owner's laptop already runs a per-user Python and has installed compiled wheels, so default AppLocker executable rules are not in force there. DLL-level policies (WDAC, Smart App Control, Defender's prevalence rule) remain unverified until the M0 probe runs. AppLocker, WDAC and Smart App Control can block unsigned DLLs from the profile. `doctor` imports every native module in a subprocess, reports which loaded and whether each is Authenticode-signed, and the app runs in **degraded mode** (pure-Python keyword search, Windows OCR, no LLM) rather than failing. The report is written for handing to IT.
- No in-house binding to llama.cpp is written. The engine wheel's only Python code is the vendored llama-cpp-python binding and the loader; the engine and core wheels declare matching version ranges.
- Builder: whether to Authenticode-sign the vendored DLLs is open; do it if the owner obtains a code-signing certificate.

### 7.5 Model weights (Call)

LLM weights cannot be on PyPI, and the owner's laptop cannot reach Hugging Face. Three supported paths, all ending in `models\` with a verified SHA-256. The first-run wizard defaults to the second, and the documentation describes the first as something to run on another machine:

1. `python -m emailsupersearch download-model <name> [--to <folder>]` using `huggingface_hub` from an ungated repository pinned by hash, honouring the system proxy and certificate store. With `--to`, it only fetches the file and its checksum into a folder for copying to the laptop. Offline mode is forced for every other code path.
2. Sideload: copy a `.gguf` into `models\`; the UI verifies the hash against the shipped manifest and accepts it. Any other `.gguf` is accepted after a warning that it is not on the manifest: the chat template is read from the file's metadata, the context size and memory footprint are estimated from the file (section 16), and its hash is recorded. This covers the PRD's Llama 3 8B Q4_0 if the owner already has it.
3. An owner-configured internal URL or share, same verification.

Embedding, NER and OCR models ship in wheels and never need downloading.

### 7.6 Upgrade, uninstall, single instance (Call)

- `python -m emailsupersearch stop` stops the host; the documented upgrade is stop, `pip install --upgrade`, start. The host refuses to start against a project DB whose schema version is newer than the code, and migrates older ones with a numbered migration table.
- `python -m emailsupersearch purge` removes the data root and the autostart entry after confirmation.
- A lock file in the data root guarantees one host per data root, so two Python installs never run two hosts on one project.

### 7.7 Licences, publishing, and the service layer (Call)

- `emailsupersearch` is GPL-3.0-or-later (C8). `emailsupersearch-engine` carries MIT for the llama.cpp and llama-cpp-python code it vendors and Microsoft's redistribution terms for the C runtime DLLs. `emailsupersearch-models-en` passes through Apache-2.0 (MiniLM) and MIT (the spaCy model). Each wheel bundles its LICENSE files.
- The owner owns the PyPI account and the three distribution names. The builder sets up CI trusted publishing and a release recipe that builds and uploads the three wheels together, and drafts the PyPI project-size-increase request for the owner to submit in the first week, because it gates the first engine release.
- The Python service layer (projects, sync, search, chat, artefacts, export) lives in its own package, separate from the web handlers and the CLI, so a public API can be documented later without rework. Documenting it is not a v1 deliverable.

## 8. Data model

### 8.1 Project (Owner for the shape; Call for the fields)

A project has: id, name, one or more source folders (local path or UNC), language (default `en`), embedder id and dimension, chunker version, LLM model selection, flags (OCR on, LLM enrichment on, group by chain default), created and last-synced timestamps. The embedder id and chunker version are stored in the project header; changing either marks the project for full re-indexing rather than mixing vectors.

### 8.2 Tables in `project.sqlite` (Call; exact DDL is Builder)

| Table | Purpose |
|-------|---------|
| `folders` | watched roots, kind (local or network), last scan id, reachable flag |
| `files` | manifest: path (PK), size, mtime_ns, sha256, file_id hint, message_key, parse_status, status_reason, last_seen_scan_id, tombstoned_at |
| `messages` | one per logical email: message_key, message_id, sent_ts (UTC epoch), received_ts, subject, normalized_subject, sender_name, sender_addr, chain_id, chain_depth, body_text, body_html_path or blob, new_text (body with quoted replies removed), has_attachments, flags |
| `recipients` | message_key, kind (to, cc, bcc), name, addr |
| `attachments` | message_key, ordinal, filename, mime, size, kind (pdf, docx, xlsx, pptx, image, msg, eml, other), extract_status, ocr_status, page_count, text_chars |
| `chunks` | chunk_id, message_key, part (`body` or `att:<ordinal>`), idx, text, token_count |
| `chunks_fts` | FTS5 external-content index over `chunks.text` (unicode61 with `tokenchars '-_#@'`). The full stop is not a token character: with it, a sentence-final `summary.` would be a different token from `summary`; an address such as `jane.doe@example.com` matches as a phrase instead, and identifiers keep `-`, `_`, `#` and `@` |
| `chunks_trigram` | FTS5 trigram index for substring matches on identifiers |
| `chunks_vec` | sqlite-vec `vec0` table: chunk rowid, float32[384], partition by message_key where useful |
| `chains` | chain_id, root_message_key, subject, first_ts, last_ts, message_count, participants_json |
| `chain_summaries` | chain_id, members_hash, conclusion, decisions_json, open_items_json, model, created_at |
| `nodes`, `edges` | knowledge graph (section 13) |
| `entity_mentions` | node_id, message_key, span, source (`ner`, `rule`, `llm`) |
| `chats`, `chat_messages` | chat history per project with scope, citations and model used |
| `artefacts` | id, kind, title, spec_json, source_message_keys, rendered_paths, model, created_at |
| `jobs` | durable job queue: kind, payload, state, priority, attempts, last_error |
| `schema_version`, `settings` | migrations and per-project settings |

### 8.3 Identity of a message (Call)

`message_key` = the RFC Message-ID when present, else SHA-256 of (lower-cased sender address, sent timestamp, normalized subject, first 2 KB of body). The file path is never identity: Outlook names files by subject, stamps the modified time with the drop time, and re-exports are byte-different. Several paths may map to one `message_key`; the message is indexed once and all paths are kept.

## 9. Ingestion

### 9.1 Discovery and sync (Owner: 1:1 mirror; Call: mechanism)

- **Reconcile scan** at host start and every 15 minutes (configurable per project): `os.scandir` each root, compare (size, mtime_ns) with the manifest, hash only changed or new files, stamp every seen row with the scan id. Rows not seen are tombstoned; a tombstone is purged, and its chunks, vectors, FTS rows, graph edges and chain membership removed, only after the **next** scan confirms the file is still gone. If a root is unreachable or `stat` fails on it, the scan aborts without touching tombstones.
- **Live trigger**: `watchfiles` on each local root, debounced to 2 seconds, triggers a rescan of the changed subtree. Events are never applied directly to the index. Network roots use polling. The owner's archive is on the local disk, so the live watcher plus reconcile is the day-one path; network roots are supported but secondary.
- Both `.msg` and `.eml` are ingested (new Outlook writes `.eml` on drag-and-drop).
- A rename detected by file-id hint updates the path without re-parsing.

### 9.2 Parsing (Owner: extract-msg; Call: the rest)

- extract-msg behind a thin adapter that adds conversation index, conversation topic and References, normalises dates to UTC epoch with the fallback chain sent date, received date, header Date, file mtime (recording which was used), and always opens files with the context manager and delayed attachments so handles are released.
- Parse workers run in a subprocess pool (1 to 2), each file under a 120-second timeout and a memory cap; a failing file ends in `parse_status = failed` with a reason, never stalls the queue.
- Nested `.msg` and `.eml` attachments are recursed to depth 3 and indexed as attachments of the parent (own chunks, own row), not as separate messages. Builder may revisit if the owner later wants them as first-class messages.
- Encoding: on decode errors retry with chardet; RTF-only bodies go through extract-msg's de-encapsulation with a regex fallback.

### 9.3 Attachments and OCR (Call)

| Type | Extractor | Notes |
|------|-----------|-------|
| PDF | pypdfium2 text per page; pages with fewer than 20 characters are rasterised at 200 dpi and sent to OCR | PRD heuristic kept |
| DOCX | python-docx: paragraphs, tables, headers and footers | |
| XLSX / XLSM | openpyxl read-only, cell values and sheet names, capped at 50k cells | |
| PPTX | python-pptx text frames, tables, notes | |
| PNG, JPG, TIFF, BMP, WebP | Pillow, every frame, downscaled to the OCR engine's maximum | |
| MSG, EML | recursion (9.2) | |
| everything else, encrypted, oversized (> 25 MB), password-protected | filename and metadata only, status `unsupported` with reason | no archive extraction in v1 |

OCR runs in its own worker process and in a low-priority queue after text extraction, so messages are searchable immediately and attachment chunks are added or replaced when OCR finishes. Engine order: Windows OCR (`Windows.Media.Ocr`, zero download, chosen when its available languages include the project language), then rapidocr if installed. Per attachment: at most 50 pages and 10 minutes. Inline images referenced by `cid:` in the HTML body are skipped unless larger than 100 KB.

### 9.4 Content hygiene (Call)

- `new_text` holds the body with quoted earlier messages removed when the quoted parent exists in the chain; the full body is kept for display. Only `new_text` is embedded and sent to NER, so a 12-message chain does not embed the first message 12 times.
- Signatures and legal disclaimers are stripped from `new_text` with a rule-based heuristic (Builder: a talon-style rule set is sufficient; no model).
- Every file and attachment ends in a known status, and the UI shows how many messages are only metadata-indexed.

### 9.5 Chunking and embedding (Call)

- Chunks of about 220 tokens with roughly 15% overlap, split on paragraph then sentence boundaries. The body and each attachment are chunked separately. Each chunk is prefixed with one context line, "From <sender> on <date>: <subject> [<part>]", which is embedded with it.
- Embedder: int8 ONNX all-MiniLM-L6-v2 via onnxruntime and `tokenizers`, batch 32, a thread pool sized from free RAM (default 4). The 256-token input limit is why chunks are small.
- Vectors are stored float32 in sqlite-vec; brute-force search is sufficient to about 500k chunks. With 50k to 100k emails and attachments, that ceiling is real, so each attachment is capped at 64 chunks (about 30 pages; the remainder is keyword-indexed only and the attachment is flagged "partially embedded"), and the project shows an alert when its chunk count passes 400k. An HNSW tier stays out of v1 unless `bench` on a corpus shaped like the owner's shows query p95 above 500 ms, in which case it becomes an M4 item behind a `[bigindex]` extra.
- Stored in the same `project.sqlite` as everything else, so a project is one file plus its exports.

### 9.6 Chains (Call)

`chain_id` comes from the 16-byte GUID in PidTagConversationIndex when present; else from In-Reply-To and References; else from normalized subject plus overlapping participants within 30 days. Reply depth comes from the index length. Chain rows are maintained incrementally.

## 10. Search and retrieval

### 10.1 Query path (Call)

1. Translate the left-pane filters into SQL over `messages` and `recipients`: From is equality on the normalised address (OR across chips), To/CC is "any recipient of kind in (to, cc) with address", date is a range on `sent_ts`, attachment types join `attachments.kind`. The result is a candidate set of message keys.
2. Run keyword search (FTS5 BM25 over `chunks_fts`, plus a trigram query when the text looks like an identifier) and vector search (sqlite-vec kNN restricted with `rowid IN (...)` from step 1), 50 chunks each.
3. Fuse with reciprocal rank fusion (k = 60) at chunk level, group to messages (score = best chunk), then to chains when "group by chain" is on (score = best message). Page 20 at a time.
4. If the project has a graph and the query mentions a known person, organisation or entity, add messages from a bounded two-hop neighbourhood as a third ranked list into the fusion (section 13.4).

Without `[nlp]`, step 2 is keyword-only and the UI says so.

### 10.2 Result card (Owner: content as mocked; Call: details)

A chain card shows: subject, message count and date span, From / To / CC (names, with addresses on hover), the cached chain conclusion if one exists (otherwise a "Summarise chain" action when the LLM is available), attachments with type and OCR status, related chains (shared participants, shared attachments by hash, graph neighbours), and actions: Open in Outlook, Add to chat, Show in graph, All N emails. A single-message card is the same without the chain parts and with a snippet of the matching chunk.

"Open in Outlook" calls a host API that runs `os.startfile` on the file path; "Reveal in Explorer" is the fallback when no `.msg` handler is registered. The host detects the `.msg` handler at startup and shows a diagnostic if none exists.

### 10.3 Chain conclusions (Owner: wanted; Call: how)

A conclusion is produced by the LLM from the chain's `new_text` in date order (latest messages first if the budget is exceeded), returning JSON with `conclusion`, `decisions[]` (item, owner, date, source message) and `open_items[]`. It is cached in `chain_summaries` keyed by a hash of the member message keys, so it is recomputed only when the chain changes. It is generated on demand from the card, or ahead of time by an opt-in job over chains matching a saved search. The same JSON feeds the decisions table artefact.

### 10.4 Excel export (PRD 4.4; Call: openpyxl only)

openpyxl writes one sheet: bold frozen header, auto-filter, wrapped preview column, widths capped at 60, a hyperlink column built with `Path.as_uri()` styled as a hyperlink, and a plain path column. Exports go to the project's `exports\` folder by default, with a "Reveal in Explorer" action; the user may choose another export folder in Settings, including a OneDrive-synced Documents folder, because exports are write-once files meant to be pasted into the owner's own work. The OneDrive refusal of 6.2 applies to the data root only. The docs state that Office may show a hyperlink warning that only the user (an HKCU setting) or IT can suppress; the library never edits the registry.

### 10.5 What "the selection" means (Call)

Chat scope, Export to Excel and every artefact act on the **selection**:

- the chains or messages the user has pinned (a checkbox on each card, or Add to chat), if any are pinned;
- otherwise the whole current result list, meaning the re-executable query plus filters, not only the visible page.

A filter-only search with no query text has the whole candidate set as its result list, sorted by date. Caps: Excel export writes at most 5,000 messages and says so; chat retrieves its top 30 chunks over the selection however large it is; the decisions table and the written brief need chain conclusions, so when the selection holds more than 10 chains the UI shows how many conclusions must be generated, with a time estimate, and asks before starting; the timeline takes exactly one chain, so with several in the selection the user picks one; the people map takes the whole selection up to the 500-node cap. The Artefacts tab is both the list of saved artefacts and the generator: a New artefact action shows the current selection summary, the four kinds, and a free-form instruction field for the brief.

### 10.6 Reading a message in the app (Call)

A read-only message viewer opens from any card or citation: headers, To and CC, the body as sanitised HTML (scripts, external references and inline `cid:` images removed in v1, plain text as the fallback), the attachment list with Save as, and the chain's other messages in order. It exists because new Outlook does not register as the `.msg` handler on many machines, so Open in Outlook cannot be the only way to read a source. The `body_html` column of 8.2 is this viewer's input. Open in Outlook and Reveal in Explorer remain for the original file.

## 11. Chat

### 11.1 Contract (PRD 4.5 restated for chunks; Call)

- Scope: the current result set by default; "Add to chat" pins chains into scope; a toggle widens scope to the whole project.
- Retrieval: the hybrid query of section 10 over the scope, top 30 chunks, grouped by message.
- Packing: context budget = model context (8192 for the 4B model, 4096 for the 8B option) minus system prompt, minus the previous turn, minus a 512-token answer reserve. Messages are packed in score order, each as a block headed `[n] <sender>, <date>, <subject>` with its best chunks; blocks are truncated at chunk boundaries, never mid-sentence.
- Instruction: answer only from the numbered blocks; cite as `[n]` after each claim; if the blocks do not contain the answer, say so plainly and say what would be needed.
- Only the previous user and assistant turn is included by default; the user can pin more.
- Tokens stream to the UI over server-sent events. Citations render as "sender, date, Open in Outlook" links. The answer also states what the results do not contain when relevant (as in the mockup).
- Chat history is stored per project with the scope and citations, so an old answer can be re-opened with its sources.

### 11.2 Acceptance (Call)

A golden set of 30 questions over a synthetic `.msg` corpus (Builder generates it) measures citation accuracy, hallucination rate and refusal rate for each model tier. The default model is confirmed only if the 4B model meets the thresholds in section 20; otherwise the default becomes the 8B at 4k context and the memory table is updated.

If the thresholds are still missed, the sanctioned remedies, in order, are: retrieval-side work first (chunking, prefilters, re-ranking, packing), because most grounding failures are retrieval failures; then the 8B model at 4k context, accepting that OCR and embedding pause while it is loaded and rewriting criterion 10 for it; then adding a model to the manifest. If none reaches the bar, v1 still ships, with chat, the brief and the decisions table labelled as preview and the measured scores shown in Settings, rather than holding the release.

## 12. Artefacts

### 12.1 Catalogue (Owner: all four)

| Kind | Content | Formats |
|------|---------|---------|
| Chain timeline | each email in the selected chain(s) as a dated point with sender and a short label of what changed | SVG; PNG made in the browser from the SVG |
| Who-talked-to-whom map | people and teams in the selected results, edges weighted by message count, busiest exchange emphasised | SVG; PNG in the browser |
| Written brief | summary, status update or draft reply from the selected results, with a sources list; free-form instruction allowed | Markdown; Word via python-docx; copy to clipboard |
| Decisions and open items table | decision, owner, date, status, source, from the chain conclusions of the selection | Excel via openpyxl; Markdown |

### 12.2 How they are made (Call)

The model never emits SVG or binary formats. Each kind has a small JSON spec:

- `TimelineSpec { events: [{ ts, actor, label, message_key }] }`
- `PeopleMapSpec { nodes: [{ id, label, kind }], edges: [{ a, b, count }] }`
- `BriefSpec`: Markdown text with `[n]` citation tags that must resolve to selected messages; unresolved tags are rejected and the model is asked once more
- `DecisionsSpec { rows: [{ item, owner, date, status, message_key }] }`

The timeline and people-map specs are built deterministically from headers and chains, and the LLM, when available, only improves the event labels. The brief and decisions specs need the LLM. Renderers are templates owned by the library (SVG string templates, python-docx, openpyxl, Markdown), so output is always well-formed and consistently styled.

Every artefact is saved under the project's `exports\` with its spec, source message keys, model name and timestamp, listed in the Artefacts tab, and can be regenerated from the same inputs. A free-form "make me a …" request in chat produces a written brief.

## 13. Knowledge graph

### 13.1 Deterministic build (Call; replaces PRD 4.6's LLM-at-ingest)

Nodes: person (merged by address, with display-name aliases from recipient tables), organisation (email domain), message, chain, attachment (by content hash), entity (from NER and rules). Edges: SENT, TO, CC, REPLIES_TO, IN_CHAIN, HAS_ATTACHMENT, MEMBER_OF_DOMAIN, MENTIONS, CO_MENTIONED (weighted by count). Built incrementally in the sync pipeline and removed with the message on prune. Stored in `nodes` and `edges`; loaded into networkx only for algorithms.

### 13.2 Entities (Call, needs `[nlp]`)

spaCy `en_core_web_sm` over `new_text` for PERSON, ORG, GPE, DATE, MONEY, plus an EntityRuler with configurable patterns for office identifiers (invoice, PO, ticket and project codes). Alias resolution: exact address match first, then display-name match, then fuzzy name match above a conservative threshold (Builder: difflib or rapidfuzz).

### 13.3 LLM enrichment (Call; PRD 4.6's intent, made opt-in)

An opt-in per-project job extracts typed relations from each message's `new_text` with the local LLM and adds `RELATES(label)` edges with incrementing weights. It is resumable, shows progress and an estimate, runs only when the chat is idle, pauses when a chat request arrives, and can be limited to a saved search. The UI shows the estimate before starting because it costs hours per thousand messages on this hardware.

### 13.4 GraphRAG retrieval (Call)

Entities and people in the query are matched to nodes (NER on the query, then fuzzy label match); a two-hop neighbourhood capped at 500 nodes is expanded; personalised PageRank on that subgraph ranks messages by summed node score; the top messages enter the fusion of section 10.1 as a third list. No LLM call at query time.

### 13.5 Graph tab (Call; replaces pyvis)

vis-network (Apache-2.0 or MIT) bundled in the web UI, fed JSON by the host. Views: the neighbourhood of the current results, or a chosen person, organisation or entity. The host caps every view at 500 nodes (PRD 5.1) before sending JSON, with clustering of domains when the cap is hit. Clicking a message node opens its card; clicking a person filters the results.

## 14. Web UI

### 14.1 Layout (Owner: Layout A)

- **Left pane**: project switcher with folder count, message count and last sync time; search box; From chips; To/CC chips; date range; attachment-type chips; "group by chain" and "only chains with a conclusion" switches; saved searches; sync status line; New project and Settings.
- **Main area, tabs**: Results (cards of section 10.2, sort by relevance or date, Export to Excel), Chat (section 11), Graph (section 13.5), Artefacts (section 12, list and viewer).
- Status badges for missing extras and degraded mode appear in the left pane's status line, never as modal errors.

### 14.2 Project creation and settings (Call)

New project: name, add folders (text field with validation plus a native folder picker opened by the host through tkinter in a subprocess), language, OCR on or off, LLM model (if `[llm]`), enrichment on or off. The first run shows a short wizard: doctor summary, create project, pick folders, optionally download a model, then the sync starts and the Results tab shows progress.

Settings: data root (validated as in 6.2), autostart toggle, port, model selection and download, OCR engine order, enrichment, log level (INFO redacted by default, DEBUG full), reconcile interval, "stop host", "purge".

### 14.3 Implementation (Call)

- Server: Starlette on uvicorn, bound to 127.0.0.1, default port 8765 with fallback to a free port; API docs disabled; JSON API plus SSE for streaming and job progress; WebSockets not required in v1.
- Security: a per-launch token exchanged once for an HttpOnly, SameSite=Strict cookie; every non-GET request requires the cookie plus a custom header; Host allow-list of loopback names; any present Origin must be a loopback origin; `nosniff` and frame headers. Same origin for page and API so browser local-network prompts do not apply.
- Front end: a single-page app compiled in CI at wheel-build time and shipped as package data; the end user never needs Node. Builder chooses between Preact or Svelte with Vite, or htmx with Alpine; the budget is 2 to 5 MB of static assets. No CDN references of any kind; fonts are system fonts.
- Launch: `python -m emailsupersearch` starts or focuses the host and opens the default browser with the tokenised URL; the URL is printed once as a fallback and never written to logs.
- Optional later extras, not v1: a chromeless window via Edge `--app=` and a tray icon.

## 15. Background host and autostart (Call; replaces PRD 4.7)

- The host process is the sync. Sync runs while the host runs; the UI shows whether the host is running and when it last reconciled.
- "Start at logon" places a shortcut to `pythonw.exe -m emailsupersearch --headless` in the per-user Startup folder (HKCU Run as the alternative). No Task Scheduler, which standard users cannot reliably use for logon tasks.
- In headless mode file descriptors 1 and 2 are redirected to the log before any native library loads, because `pythonw` has no console and llama.cpp and onnxruntime write to C-level stderr; llama.cpp's logger is routed to Python logging and onnxruntime's severity is set explicitly.
- A tray icon is not in v1; the headless host is reached by running `python -m emailsupersearch` again, which focuses the existing instance's URL.

## 16. Hardware and memory governance (Call)

| Component | Resident | Rule |
|-----------|----------|------|
| Host, web server, SQLite | ~300 MB | always |
| Embedder ONNX session | ~300 MB | loaded on first embed, kept |
| OCR worker | ~100 MB (Windows OCR) to ~500 MB (rapidocr) | one worker, exits when the queue is idle for 5 minutes |
| LLM worker, Qwen3-4B Q4_K_M, 8k context | ~3.7 GB | loaded on first chat or job, unloaded after 10 idle minutes |
| LLM worker, Llama 3.1 8B Q4_K_M, 4k context | ~5.4 GB | optional model, same rules |
| Browser tab with graph view | 0.5 to 1 GB | 500-node cap |

For a model not in the table, the footprint is estimated as file size plus a KV cache computed from the GGUF metadata (layers, heads, chosen context), and the same rules apply to the estimate.

Rules: one LLM worker, serving chat or enrichment but never both; enrichment pauses on chat; OCR and embedding do not run while the 8B model is loaded on a 16 GB machine; worker counts are derived from free RAM at start; the default model is the 4B whenever physical RAM is 16 GB or less.

Default LLM: Qwen3-4B-Instruct (Apache-2.0), Q4_K_M. Optional: Llama 3.1 8B Instruct, Q4_K_M, 4k context. Engine: llama.cpp through the vendored engine wheel, Vulkan first, CPU fallback (7.4). Expect roughly 20 tokens per second on the 4B and 10 to 12 on the 8B when Vulkan works, half that on CPU; these are proxies from similar hardware and must be measured (section 19).

## 17. Zero-network and privacy (PRD 5.2; Call)

- Set at import time, before any dependency loads: `HF_HUB_OFFLINE=1` (lifted only inside `download-model`), `HF_HUB_DISABLE_TELEMETRY=1`, `HF_HUB_DISABLE_UPDATE_CHECK=1`, `ORT_DISABLE_TELEMETRY=1` plus `onnxruntime.disable_telemetry_events()`. No chromadb, no pyvis, no CDN assets, API docs disabled.
- CI runs a full ingest, search, chat and artefact pass with a socket guard that fails the build on any connection to a non-loopback address. This is the enforcement of the zero-telemetry requirement, not documentation.
- Logs: one rotating handler per process (5 MB, 3 backups) under `logs\`. At INFO, subjects, paths and addresses are hashed or truncated; full detail only at DEBUG behind an explicit toggle that the UI labels as sensitive. The `doctor` report contains environment facts only, never message data.
- Exports and artefacts contain message content by design and are written only to the project's `exports\` folder.

## 18. Diagnostics and benchmarking (Call)

- `python -m emailsupersearch doctor`: Python build and prefix (Store Python detected and warned), site-packages path headroom, C runtime DLLs, Vulkan loader and device, each native module imported in a subprocess with signer status, Windows OCR languages, free RAM and disk, data root sanity, autostart state, `.msg` handler. Produces a redacted text report.
- `python -m emailsupersearch bench`: parse, OCR and embed rates on a synthetic corpus, query p50 and p95, LLM prompt and generation tokens per second per engine mode, peak memory. This is the first engineering deliverable, run on the real laptop with Defender active, and its numbers set the defaults in sections 9.5, 11 and 16.

### 18.1 Delivery order and the laptop loop (Call)

Development happens on the owner's own machine, not on the office laptop. Every fact about the laptop is learned through owner-run steps that produce one redacted report to paste back, so the plan front-loads a probe and stages delivery so that each stage is usable on the laptop before the next starts:

- **M0 Probe.** A tiny pure-Python wheel containing only `doctor`, a CPU `bench` of parse and embed, and one native import test per planned dependency. Its report settles 7.3, 7.4 and the policy rows of section 19 before anything heavy is built.
- **M1 Search.** Ingest, sync, chunking, embedding, hybrid search, filters, chains, Excel export, the message viewer, and layout A with the Results tab. Usable daily from here; schema migrations and stop-upgrade-start are real from this milestone.
- **M2 Engine and chat.** The engine wheel, model download and sideload, chat with citations, chain conclusions, the written brief, the decisions table, the golden set, and `bench` of the LLM path on the laptop.
- **M3 Graph and remaining artefacts.** Deterministic graph, entities, the Graph tab, the timeline and people map, and the LLM enrichment job.
- **M4 Hardening.** Autostart, `doctor` polish, the zero-network CI test, and the acceptance pass.

The ten acceptance criteria gate the final v1. Interim builds are dogfooded by the owner and may miss criteria that belong to later milestones.

Owner inputs the plan depends on: before M1, a folder of a few dozen real or redacted `.msg` and `.eml` files with typical attachments, RTF-only bodies, nested messages and meeting items, for the parser test corpus (the development machine has no Outlook to generate them); at M0 and M2, running the probe and `bench` on the laptop and pasting back the report; at M2, copying the model file onto the laptop.

## 19. Risks and what must be verified on the target laptop

| Risk | Why it matters | Verification |
|------|----------------|--------------|
| Vulkan cannot allocate a 3 to 5 GB model on the iGPU's BIOS frame buffer | GPU path fails or is slow | `bench` on the laptop; the loader's fallback chain is the mitigation |
| Real tokens per second lower than proxies | model default, enrichment estimates | `bench` |
| AppLocker or WDAC blocks DLLs or the per-user `python.exe` | nothing native loads | `doctor` on the laptop before any further work |
| C runtime DLLs missing and app-local loading blocked | native imports fail | `doctor` |
| Windows OCR language not installed and `[ocr]` not allowed | scanned pages unreadable | `doctor` lists OCR languages |
| Office hyperlink warning or wrong `.msg` handler | Excel links annoying or broken | try one export on the laptop; in-app Open in Outlook is primary |
| Firewall prompt on loopback bind | first launch confusing | observe first launch; expected none |
| extract-msg is 0.x and may change between minors | parser breakage on upgrade | pin `>=0.56,<0.57` and run the parser test corpus in CI |
| 50k to 100k attachment-heavy emails exceed brute-force vector comfort | slow queries | `bench` on a corpus shaped like the owner's; per-attachment chunk cap and the 400k-chunk alert are the mitigation, HNSW the contingency |
| Model weights must travel by hand because Hugging Face is blocked | first chat delayed, wrong file imported | `download-model --to` on another machine, hash check on import, wizard defaults to import |

## 20. Acceptance criteria

1. On a clean Windows 11 standard-user account with per-user Python 3.12, the documented install and first launch succeed with no elevation prompt, and the UI opens in the default browser.
2. With networking disabled after install and model download, every feature in section 3 works; the CI socket guard passes.
3. A folder of 10k mixed `.msg` and `.eml` files with attachments is fully indexed, and adding, renaming, replacing and deleting files is reflected within one reconcile interval with no orphaned rows.
4. Sender, To/CC, date and attachment filters combine correctly with semantic and keyword queries; an invoice-number query finds the message by exact match.
5. A scanned PDF attachment is searchable by its OCR'd text within the OCR queue's completion.
6. On the golden set, the default model cites a correct source for at least 90% of answerable questions and refuses at least 90% of unanswerable ones; no answer cites a message outside the scope.
7. Each of the four artefacts renders from a selection, opens in its target application, and lists its source messages.
8. The graph tab shows a neighbourhood for a chosen person within 2 seconds for a 100k-message project, never exceeding 500 nodes.
9. `doctor` on a machine without the engine wheel or without model weights reports degraded mode and the UI shows the corresponding badges while search still works.
10. With the 4B model loaded and a reconcile scan running, the UI remains responsive and total process memory stays under 6 GB.

## 21. Left to the builder

- Exact SQLite DDL, indexes and migration tooling.
- The front-end framework (Preact or Svelte with Vite, or htmx with Alpine) and component structure.
- The chunker implementation and the signature and quote-stripping rules.
- Prompt wording for chat, chain conclusions, brief and decisions, and the JSON schemas' field details.
- SVG templates and visual style of the artefacts, consistent with the mockups.
- Job queue implementation, worker protocol and backpressure.
- Synthetic test corpus generator and the golden question set.
- CI: wheel-only resolution matrix for 3.12 and 3.13 on win_amd64, socket-guard test, parser corpus test, wheel-content check for the compiled UI.
- Whether and how to sign vendored DLLs.
- Repository layout, packaging backend (hatchling recommended for its `artifacts` handling of the compiled UI), and release process for the three distributions.

## 22. Out of scope but anticipated

PST and OST import (an alpha-quality pip library exists), multilingual models as a separate models wheel with per-project language, an HNSW tier past 500k chunks, a tray icon and app window, a Linux or macOS port, and a colleague-facing install guide once the owner has lived with v1.
