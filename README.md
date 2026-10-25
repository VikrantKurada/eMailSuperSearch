# eMailSuperSearch

eMailSuperSearch is a pip-installable, fully offline search tool for Outlook `.msg` and `.eml` archives on Windows. It indexes the messages and their attachments (PDF, Word, Excel, PowerPoint and scanned images via OCR) into a local database and serves a browser interface on `127.0.0.1` only, so nothing leaves the machine and no administrator rights are needed.

## Quick start

Requires Python 3.12 or 3.13 (3.12 recommended), installed per-user from python.org with "use admin privileges when installing py.exe" unticked. Neither the install nor the app needs administrator rights.

```bat
py -3.12 -m venv %LOCALAPPDATA%\eMailSuperSearch\venv
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all]"
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch
```

The two packages named in `--no-binary` are pure-Python dependencies of `extract-msg` that exist only as source archives; they build without a compiler. Every other dependency installs from a wheel. Launch is always `python -m emailsupersearch`; console scripts are not on `PATH` for per-user installs.

The full procedure is in [docs/install.md](docs/install.md): requirements, sideloading the model wheel, the first run, where the data lives, an offline bundle for a laptop that cannot reach PyPI, upgrading and purging, and troubleshooting.

## Probe (M0)

This section describes the pre-release probe (version 0.0.1), kept for reference; for an ordinary install use the Quick start above. The probe answers, on your own laptop, whether every native dependency loads under your company's security policy and how fast parsing, embedding and nearest-neighbour search run, so the defaults can be set from measurements. Both commands print facts about the machine and speed figures only, never message text.

1. Install Python 3.12 for the current user from python.org ("Install Now") and untick "use admin privileges when installing py.exe" (the py launcher for all users). No administrator rights are needed.
2. Create the environment:

   ```bat
   py -3.12 -m venv %LOCALAPPDATA%\eMailSuperSearch\venv
   ```

3. Install the probe into it:

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all]"
   ```

4. Run both checks:

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch doctor
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch bench --json
   ```

5. Paste both outputs back.

`bench` builds a synthetic mail corpus in a temporary folder and, with the default 300,000 vectors, takes a few minutes on a slower laptop. For a quicker first run add `--vectors 50000`. To time your own mail instead, add `--corpus FOLDER` (every `.eml` and `.msg` under it is read; nothing is changed or copied).

## Licence

GPL-3.0-or-later. See `LICENSE`.
