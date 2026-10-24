# eMailSuperSearch: PRD parse and risk register

Date: 2026-10-23. Source: `eMailSuperSearch_PRD.txt` (status "Final") plus the delivery constraints the owner added in conversation. Evidence for every "finding" column is in [2026-10-23-prd-feasibility-research.md](2026-10-23-prd-feasibility-research.md).

Status key: **OK** meets the constraints as written. **CHANGE** the intent holds but the named library or mechanism must change. **AT RISK** cannot be met as written. **BROKEN** fails even on the PRD's own terms.

## Constraints added by the owner (not in the PRD file)

| ID | Constraint |
|----|------------|
| C1 | Delivered as a Python library on PyPI. The only thing the end user installs is this library via pip. |
| C2 | Modular install: users can opt out of components (LLM engine, LLM weights, OCR, graph, ...). The rest must keep working. |
| C3 | Web UI served locally by the library, 100% offline, no install beyond the pip package. Users create projects for their searches. A search pane sits on the left. |
| C4 | Runs on a corporate office laptop: standard user, no admin rights, cannot install software. |
| C5 | Background sync keeps the index a 1:1 mirror of the mail folder. |

## Requirements inventory

| ID | PRD | Requirement | Status | Finding |
|----|-----|-------------|--------|---------|
| R1 | 4.1 | Incremental sync via filename-set diff | AT RISK | Classic Outlook names dropped files by subject, stamps mtime with the drop time, and prompts to overwrite same-subject files. New Outlook drag-drop writes `.eml`, not `.msg`. Identity must come from Message-ID or a metadata hash in a manifest, with periodic full reconciliation. |
| R2 | 4.1 | Extract sender, subject, path, date; dates as Unix timestamps | OK | extract-msg exposes all fields. Dates arrive tz-aware in local time; normalise to UTC epoch. `date` is None for unsent drafts, so fall back to receivedTime, header Date, then mtime. |
| R3 | 4.1 | Prune vector and graph records when a `.msg` is deleted | AT RISK | File watchers silently drop events on buffer overflow and UNC shares vanish on VPN drops. Needs two-phase deletes, a root-reachability check, and a scheduled full scan. |
| R4 | 4.2 | PDF and DOCX text via PyMuPDF and python-docx | CHANGE | PyMuPDF 2.0 is AGPL-3 or commercial and 63 MB. pypdfium2 (BSD, 4 MB) extracts text and rasterises pages. python-docx is fine. |
| R5 | 4.2 | OCR of scanned PDFs and images via easyocr | CHANGE | easyocr pulls torch (about 250 MB of wheels) and downloads 91 MB of weights from GitHub on first use. Windows' built-in OCR (about 1 MB of wheels, 0.13 s per page measured) plus rapidocr (bundled Apache-2 models, 2.6 s per page measured) as the fallback. |
| R6 | 4.2 | Append attachment text to the body and embed once | BROKEN | The default MiniLM embedder truncates at 256 word pieces, so appended attachment text is never embedded. Chunk-level indexing with parent-email grouping is mandatory. |
| R7 | 4.3 | Natural-language vector search | OK | An int8 ONNX MiniLM (23 MB) can ship inside a PyPI wheel. Brute-force search over about 300k chunks answers in tens of milliseconds. |
| R8 | 4.3 | Exact sender filter and date range filter | OK | Plain SQL over a metadata table. |
| R9 | 4.3 | Compound `$and` / `$or` logic | OK | Plain SQL. The Chroma DSL is not needed, and Chroma's own hybrid search is Cloud-only. |
| R10 | 4.4 | Excel export via pandas and openpyxl | OK | openpyxl alone does it; pandas adds 11 MB for nothing. |
| R11 | 4.4 | Bold headers, wrapping, column widths | OK | openpyxl primitives. |
| R12 | 4.4 | `file:///` links that open the `.msg` in Outlook | PARTIAL | Links work, but Office shows a "hyperlinks can be harmful" prompt on each click unless the user sets an HKCU key, and which Outlook opens the file depends on the association. An in-app "Open in Outlook" button is the reliable path. |
| R13 | 4.5 | Local LLM via gpt4all | AT RISK | gpt4all's PyPI package is frozen at 2.8.2 (Aug 2024); the repo has had no maintainer activity since Feb 2025. llama-cpp-python is the only maintained pip engine with Vulkan on this iGPU, but its Windows wheels live on a GitHub Pages index, not PyPI. The library would publish its own engine wheels (42 MB fits PyPI's limit). |
| R14 | 4.5 | Inject the top 5 emails into the system context | CHANGE | Five whole emails with attachments exceed a 4k or 8k context. Retrieve chunks, group by email, pack to a token budget. |
| R15 | 4.5 | Strict grounding with sender and date citations, refusal otherwise | OK | Needs a golden question set to measure citation accuracy and refusal rate per model tier before the default model is chosen. |
| R16 | 4.6 | LLM triple extraction for every email during ingestion | AT RISK | Measured-proxy throughput is 300 to 480 emails per hour on the iGPU, so 20k emails take 42 to 67 hours of continuous GPU time. A deterministic graph from headers plus spaCy NER builds in minutes. LLM extraction becomes an opt-in, resumable enrichment. |
| R17 | 4.6 | networkx graph with weighted edges | OK | 100k nodes and 1M edges fit in 383 MB. Persist in SQLite tables so the mirror can insert and delete per message. |
| R18 | 4.6 | GraphRAG: query Chroma and networkx together | OK | Entity-link the query, take a bounded 2-hop neighbourhood, fuse with vector hits by reciprocal rank. |
| R19 | 4.6 | pyvis `knowledge_map.html` for offline exploration | BROKEN | pyvis is unmaintained since Feb 2023 and loads vis-network and Bootstrap from CDNs in every mode except `in_line`. The page is blank offline. Bundle vis-network (435 KB) inside the web UI. |
| R20 | 4.7 | Background sync via pythonw.exe and Task Scheduler | AT RISK | Non-admin `schtasks /sc ONLOGON` is reported to fail; S4U tasks need a right standard users lack. Under pythonw, stderr is closed and native libraries that write to it crash. Use an always-on host process started from the per-user Startup folder. |
| R21 | 4.7 | Rotating logs, 5 MB x 3 backups | OK | Add a redaction policy: logs will otherwise contain subjects, paths and addresses. |
| R22 | 5.1 | Vulkan offload via `device="amd"` | CHANGE | That flag is gpt4all-specific and only accelerates Q4_0/Q4_1. Probe Vulkan at runtime and fall back to CPU. Whether a 4 to 5 GB model allocates through Vulkan on a locked BIOS frame buffer is unverified and must be benchmarked on the real laptop. |
| R23 | 5.1 | LLM footprint under 6 GB | AT RISK | Llama 3 8B Q4 plus an 8k f16 KV cache is 5.7 to 6.0 GB before compute buffers. A 3 to 4B model fits in 2.5 to 3.7 GB. 8B only at 4k context or with a q8 KV cache. |
| R24 | 5.1 | Graph view capped at about 500 nodes | OK | Correct for vis-network with physics on. |
| R25 | 5.1 | Graph processing asynchronous | OK | Needs a defined job queue and single SQLite writer. |
| R26 | 5.2 | Zero telemetry, no external calls | AT RISK as shipped | Chroma's default embedder downloads 83 MB from S3, easyocr downloads from GitHub, gpt4all fetches model lists, huggingface_hub and onnxruntime phone home by default, pyvis loads CDNs. Meetable only with a single enforced checklist and a CI test that runs the stack with networking blocked. |
| R27 | 5.2 | Data in `./email_vector_db` and `~/.cache/gpt4all` | CHANGE | Relative paths depend on the launcher's CWD. Everything goes under `%LOCALAPPDATA%\eMailSuperSearch` (non-roaming, not protected by Controlled Folder Access, not under OneDrive). |

## Constraint-level risks

| ID | Risk | Finding |
|----|------|---------|
| K1 | C1: artifacts that are not on PyPI | llama-cpp-python Windows wheels (GitHub Pages), LLM weights (PyPI caps files at 100 MiB, 1 GiB by request), the MSVC runtime for Python 3.11. Resolution: own engine wheels on PyPI, a download-or-sideload step for weights, Python 3.12 floor. |
| K2 | C4: Visual C++ runtime | onnxruntime, chromadb's Rust binding, torch, llama.cpp and gpt4all all import `msvcp140.dll` and none bundles it. `vc_redist` needs UAC. `pip install msvc-runtime` (cp312+) or app-local DLLs are the no-admin remedies. |
| K3 | C4: application control | AppLocker default rules block a per-user `python.exe`. WDAC or Smart App Control can block unsigned DLLs (ggml, sqlite-vec). The library can only detect, report and degrade; a `doctor` command and a pure-Python degraded mode are required. |
| K4 | C4: Python provisioning | python.org per-user installer, the 3.14 install manager, and Microsoft Store Python all behave differently (Store Python redirects AppData writes and has a 142-char site-packages prefix that breaks torch under MAX_PATH). Launch via `python -m emailsupersearch`, never a console script. |
| K5 | C3: the "project" concept | The PRD assumes one target directory. Nothing defines what a project is, which drives storage layout, sync scope, and what the left pane lists. Owner decision. |
| K6 | C2 and C4: Python version range | Every proposed wheel exists only on 3.12 and 3.13 (networkx 3.7 needs 3.12; msvc-runtime has no 3.11 wheel; pywin32 stops at 3.13). |
| K7 | Licensing | extract-msg, the only maintained `.msg` parser, is GPL-3. python-oxmsg (MIT, 0.0.2) is the permissive alternative pending a parity check. PyMuPDF is AGPL. |
| K8 | Languages | Every default is English-only (MiniLM, spaCy en, Windows OCR profile languages). The PRD never states the corpus language. |

## Stack comparison (PRD vs research recommendation)

| Component | PRD | Recommended | Why |
|-----------|-----|-------------|-----|
| `.msg` parsing | extract-msg | extract-msg behind an adapter, or python-oxmsg if GPL is unacceptable | Only maintained parser; GPL-3 |
| PDF | PyMuPDF | pypdfium2 | AGPL, 63 MB |
| OCR | easyocr | Windows.Media.Ocr default, rapidocr fallback, isolated worker process | torch, downloads, speed |
| Vector store | chromadb | SQLite + FTS5 + sqlite-vec, embedder from a bundled int8 ONNX MiniLM | 85 packages, S3 download, VC++ import failure, no offline hybrid search |
| Keyword search | none | SQLite FTS5 fused with vectors by reciprocal rank | Exact IDs and names that embeddings miss |
| LLM engine | gpt4all | llama-cpp-python (Vulkan and CPU) vendored into own PyPI wheels, runtime probe | gpt4all unmaintained |
| LLM model | Llama 3 8B Q4_0 | Qwen3-4B-Instruct Q4_K_M default (Apache-2.0), Llama 3.1 8B optional at 4k context | 6 GB cap, licence, gating |
| Graph build | LLM triples at ingest | Headers, threads, attachments, domains plus spaCy NER; LLM enrichment opt-in | 42 to 67 h per 20k emails |
| Graph view | pyvis HTML | vis-network bundled in the web UI | pyvis offline-blank |
| Export | pandas + openpyxl | openpyxl | Drop 11 MB |
| Web UI | none | Starlette or FastAPI + uvicorn, prebuilt SPA shipped in the wheel, 127.0.0.1 with per-launch token | marimo and JupyterLab pattern, zero telemetry |
| Background sync | pythonw + Task Scheduler | Always-on host process with watchfiles plus periodic reconcile, Startup-folder autostart | Non-admin scheduler limits, pythonw stderr |
| Data location | `./email_vector_db`, `~/.cache` | `%LOCALAPPDATA%\eMailSuperSearch` | Roaming, OneDrive, CWD |
| Python | unspecified | 3.12 to 3.13 | Wheel availability |

## Decisions only the owner can make

1. What a project is (saved search over one index, its own folders and index, or a hybrid with a shared parse cache).
2. The library's licence, and therefore whether a GPL parser is acceptable.
3. Default LLM: Qwen3-4B (fits comfortably) or Llama 3.1 8B at 4k context (PRD intent).
4. Target corpus languages.
5. Whether LLM-extracted triples are a v1 must-have or an opt-in enrichment.
6. Whether GPU acceleration is a requirement or best-effort with CPU fallback.
7. How weights reach an air-gapped laptop: Hugging Face download, sideload from a share, or both.
8. Browser tab vs app window; fixed vs random port.
9. Whether sync must run when the UI is closed.
10. Scale target for v1 (100k emails per project is the natural brute-force ceiling).
