# eMailSuperSearch

eMailSuperSearch is a pip-installable, fully offline search tool for Outlook `.msg` and `.eml` archives on Windows. It indexes the messages and their attachments (PDF, Word, Excel, PowerPoint and scanned images via OCR) into a local database and serves a browser interface on `127.0.0.1` only, so nothing leaves the machine and no administrator rights are needed.

## Install

Requires Python 3.12 or 3.13 (3.12 recommended), installed per-user from python.org with "use admin privileges when installing py.exe" unticked, or with the Python install manager. Neither needs administrator rights.

```bat
py -m venv %LOCALAPPDATA%\eMailSuperSearch\venv
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all]"
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch
```

The two packages named in `--no-binary` are pure-Python dependencies of `extract-msg` that exist only as source archives; they build without a compiler. Every other dependency installs from a wheel. Launch is always `python -m emailsupersearch`; console scripts are not on `PATH` for per-user installs.

## Licence

GPL-3.0-or-later. See `LICENSE`.
