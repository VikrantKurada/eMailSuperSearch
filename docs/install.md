# Installing eMailSuperSearch

eMailSuperSearch is a Python library you install with `pip` into its own virtual environment. It needs no administrator rights, no compiler and no network after the install. This page is the whole procedure for a locked-down Windows laptop.

The package is not on PyPI yet. Each [GitHub release](https://github.com/VikrantKurada/eMailSuperSearch/releases) carries its two wheels: the application and the English model. The commands below install release v0.1.2; for a newer release, use the file names from its notes.

## Requirements

- Windows 10 or 11, 64-bit.
- Python 3.12 or 3.13, 64-bit, installed for the current user only. Python 3.12 is the tested default. Python 3.14 and older than 3.12 are not supported, and a plain `py` picks the newest Python installed, so every command below says `py -3.12`.
- A standard user account is enough. Install Python from python.org with "Install Now", and untick "Use admin privileges when installing py.exe" (the `py.exe` launcher for all users, which would need administrator rights). The Python install manager works as well. Neither needs administrator rights.
- The laptop reaches PyPI. It does not need to reach huggingface.co, because the English model ships as a wheel (see Models).
- About 500 MB of free disk for the environment (it measures about 420 MB), and about 1 GB of free memory while the host runs. The index grows with your mail.

## Install

Three lines in a Command Prompt:

```bat
py -3.12 -m venv %LOCALAPPDATA%\eMailSuperSearch\venv
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all] @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.2/emailsupersearch-0.1.2-py3-none-any.whl" "emailsupersearch-models-en @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.2/emailsupersearch_models_en-0.1.0-py3-none-any.whl"
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch
```

If the company network stops pip from downloading from GitHub, download the two wheels from the [release page](https://github.com/VikrantKurada/eMailSuperSearch/releases/tag/v0.1.2) in a browser, put them in a local folder such as `C:\ess-wheels`, and replace the middle line with:

```bat
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console --find-links C:\ess-wheels "emailsupersearch[all]"
```

What they do:

1. Create the environment in `%LOCALAPPDATA%\eMailSuperSearch\venv`. That folder is short (Windows limits paths to 260 characters), not roaming and not synced. Do not use `pip install --user` or a Microsoft Store Python.
2. Install the library with every extra (`[all]` is `[nlp,ocr]`: semantic search and the fallback OCR engine), taking the application and the model from the GitHub release and every other package from PyPI.
3. Start the host and open the browser.

`--only-binary=:all:` makes pip refuse anything that would need a compiler. The two packages named in `--no-binary` are pure-Python dependencies of `extract-msg` that exist only as source archives; pip builds them on the spot without a compiler. Every other dependency installs from a wheel.

Launch is always `python -m emailsupersearch`. Console scripts are not on `PATH` for a per-user install, and a freshly built `.exe` launcher is blocked by Defender's prevalence rule. If you would rather type less, run `%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\activate` first and use `python -m emailsupersearch` from then on.

## Models

`[all]` includes `emailsupersearch-models-en`, the English embedding model (an int8 ONNX all-MiniLM-L6-v2 and its tokenizer, about 17 MB). Without it search is keyword-only and the interface says so.

The laptop never downloads the model at run time, and it does not need to reach huggingface.co: the model ships as the `emailsupersearch_models_en-0.1.0-py3-none-any.whl` wheel on each release, and the install line above installs it. The wheel is built on a machine that can reach huggingface.co, with `python scripts/fetch_models.py` followed by `python -m build --wheel -o dist packages/models-en`.

## First run

`python -m emailsupersearch` starts the host and opens your browser on `http://127.0.0.1:8765/`. If port 8765 is taken, the host uses a free one. The address is reachable from this machine only.

- The first page asks you to "Create your first project": a name, one or more folders of `.msg` and `.eml` files (type or paste a path and press Add, or use Browse), and whether to recover text from scanned attachments (OCR). Folders are read, never changed.
- After "Create project" the host scans the folders and indexes them. The left pane shows the project, the search box and filters, and the sync status. Searching works while indexing runs; results fill in as files are indexed.
- Running `python -m emailsupersearch` again, from any window, does not start a second host. It opens a new browser tab on the one that is running.
- On a console the address is printed once. It carries a one-time launch token that starts your session, so open that address rather than typing `http://127.0.0.1:8765/` by hand.

## Where data lives

Everything is under `%LOCALAPPDATA%\eMailSuperSearch`:

| Path | Holds |
|------|-------|
| `catalog.sqlite` | the list of projects and the global settings |
| `projects\<project-id>\project.sqlite` | everything one project owns: messages, chunks, vectors, search index |
| `projects\<project-id>\exports\` | the Excel exports the project produced |
| `cache\parsed\` | parsed text and OCR output per source file, shared by all projects |
| `logs\` | rotating logs (`host.log`, 5 MB with 3 backups; `host-stderr.log` for a headless host) |
| `venv\` | the recommended install location |

Nothing leaves the machine. The host listens on 127.0.0.1 only, the libraries it uses are told not to phone home (offline mode and telemetry off, set before any of them loads), and there are no CDN assets. Every release is tested with a socket guard that fails the build if the host process opens a connection to any address other than the local machine.

Logs at the default level hide subjects, file paths and addresses behind short hashes. The DEBUG level in Settings writes them in full and is labelled sensitive. The `doctor` report holds facts about the machine only, never message text.

To keep the data elsewhere, set the data root in Settings or set the `EMAILSUPERSEARCH_DATA_ROOT` environment variable. A folder inside OneDrive, Documents or Desktop is refused, because a synced folder corrupts a live database.

## Offline bundle

Use this when the laptop cannot reach PyPI at all. Build the bundle on a connected Windows machine with the same Python version (3.12 here), copy the `bundle` folder across, and install from it.

On the connected machine, with the repository's `requirements-lock-3.12.txt`:

```bat
pip download -r requirements-lock-3.12.txt --no-deps --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console --platform win_amd64 --platform any --python-version 3.12 --abi cp312 --abi abi3 --abi none -d bundle
pip download --no-deps --only-binary=:all: setuptools -d bundle
```

Then download the two wheels from the [release page](https://github.com/VikrantKurada/eMailSuperSearch/releases/tag/v0.1.2) into the same `bundle` folder.

On the laptop, in the virtual environment from the Install section:

```bat
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --no-index --find-links bundle "emailsupersearch[all]"
```

Notes:

- The lock file lists every package, so `--no-deps` is correct. pip refuses `--platform` together with `--no-binary` unless `--no-deps` is set, which is why it is there.
- `setuptools` is in the bundle because the two source archives (`red-black-tree-mod`, `win_unicode_console`) are built on the laptop, and `--no-index` leaves pip nowhere else to get a build tool from. The library and the model package are not in the lock and not on PyPI, which is why their two wheels come from the release page. If you built them yourself, copy the two wheels from `dist` into `bundle` instead.
- The lock was frozen from the build environment, which includes the developer tools (pytest, ruff and so on), so the bundle is about 150 MB and the install uses only part of it.
- `requirements-lock-3.12.txt` and `requirements-lock-3.13.txt` are committed with the repository. CI freezes both in a fresh virtual environment on every build (the `requirements-lock-3.12` and `requirements-lock-3.13` artifacts), and a tag build pushes them to `lock/<tag>-py3.12` and `lock/<tag>-py3.13` branches. For Python 3.13 use the 3.13 file and change the flags to `--python-version 3.13 --abi cp313`.

## Stop, upgrade, purge

- Stop the host: `python -m emailsupersearch stop`, or "Stop host" in Settings. `stop` waits up to 90 seconds for the host process to exit, printing a dot every 5 seconds, and then prints "stopped" (or "not running" when there was none, or "still running" if it gave up).
- Upgrade: stop the host, wait until `stop` prints "stopped", install the new release, start it again. Upgrading while the host still runs fails or leaves a broken install, because Windows will not replace a library a running process has loaded. Take the two file names from the new release's notes; for example, for v0.1.2:

  ```bat
  %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch stop
  %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --upgrade --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all] @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.2/emailsupersearch-0.1.2-py3-none-any.whl" "emailsupersearch-models-en @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.2/emailsupersearch_models_en-0.1.0-py3-none-any.whl"
  %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch serve
  ```

  Projects made by an older version are migrated when the host opens them. A project made by a newer version than the code is left closed and reported, never changed.
- Purge: `python -m emailsupersearch purge --yes` stops the host and deletes the projects, indexes, cache and logs, or use "Purge" in Settings. It removes only what the program made, in a folder that carries its ownership marker, and anything else in the folder stays. Your mail folders are never touched. It leaves the `venv` folder; to remove the program completely, delete `%LOCALAPPDATA%\eMailSuperSearch\venv` afterwards.

## Opening mail from Excel

The primary way to open a message is the Open in Outlook button on each result in the app. When no `.msg` handler is registered, which is common with the new Outlook, click the subject instead: the built-in message viewer shows the headers, the body and the attachments in the browser.

An Excel export also holds a hyperlink to each message file. Office may show a "Hyperlinks can be harmful to your computer and data" prompt when you click a `file://` link. Only you (through a per-user HKCU registry setting) or your IT department (through policy) can suppress it; the library never edits the registry. Microsoft's own (now archived) documentation names the value `DisableHyperlinkWarning` under `HKEY_CURRENT_USER\Software\Microsoft\Office\16.0\Common\Security`. A policy set by IT takes precedence, and Protected View shows the prompt regardless. The links are a convenience; if the prompt bothers you, use the button in the app.

## Troubleshooting

- Run `python -m emailsupersearch doctor`. It checks the Python build, the Microsoft C runtime DLLs, each native module (imported in a subprocess, with its signature status), Windows OCR languages, SQLite features, memory and disk, the data root and the `.msg` handler, and prints a redacted report that is safe to hand to IT. `--json` prints it as JSON. A line marked WARN is advice, not a failure; "Degraded" names any feature that is switched off because a native module could not load.
- Logs are in `%LOCALAPPDATA%\eMailSuperSearch\logs\host.log`. A headless host (`--headless`) writes what the console would have shown to `host-stderr.log` in the same folder.
- "another instance is starting" (exit code 1): another launch holds the single-instance lock and is still starting. Wait a few seconds and run the command again. If nothing is running, the lock was left by a crash; it is recognised as stale and replaced on the next start. `python -m emailsupersearch stop` clears up a host that is half started.
- A refused data root (exit code 2) prints its reason: the folder is inside OneDrive, Documents or Desktop, is a file, or already holds files other than a Python virtual environment but no eMailSuperSearch marker, so it is not a folder the program made. Version 0.1.1 also refused the default folder when it held only the install's `venv`; upgrade to 0.1.2, or create the empty marker file with `type nul > "%LOCALAPPDATA%\eMailSuperSearch\.emailsupersearch"` and start again. Remove `EMAILSUPERSEARCH_DATA_ROOT` or point it at a new or empty folder. A refused `data_root` setting is ignored with a warning and the default folder is used.
- pip cannot reach PyPI through the company proxy (timeouts, or SSL certificate errors): give pip the proxy once with `python -m pip config set global.proxy http://proxy.example.com:8080` (your IT department has the address), and if the proxy inspects TLS add `--trusted-host pypi.org --trusted-host files.pythonhosted.org` to the install line. Without any route to PyPI, use the Offline bundle.
- The install says it cannot find `emailsupersearch` or `emailsupersearch-models-en`: the package is not on PyPI, so use the release URLs from the Install section, or download the two wheels from the release page and add `--find-links` with their folder. The install says it needs a compiler: the `--only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console` flags are missing from the command, or the Python version is not 3.12 or 3.13.
- A native module reported as blocked by `doctor` (AppLocker, Smart App Control or Defender): if it is `onnxruntime`, the engine behind semantic search, the app still runs, keyword-only, and the status line says "No semantic search". The degraded mode covers the embedder only: `sqlite-vec` (the vector index), `tokenizers` (used to cut text into chunks) and `pdfium` (PDF text) are hard requirements, and the host does not start without them. Give IT the `doctor` report.
