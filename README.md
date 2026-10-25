# eMailSuperSearch

eMailSuperSearch searches your archived Outlook mail (`.msg` and `.eml` files) on a Windows laptop. It indexes the messages and their attachments (PDF, Word, Excel, PowerPoint, and scanned images through OCR) into a local database and shows a search page in your browser. Everything stays on the laptop: the app listens on `127.0.0.1` only, sends nothing anywhere, and needs no administrator rights.

The package is not on PyPI yet. Each [GitHub release](https://github.com/VikrantKurada/eMailSuperSearch/releases) carries the two files you install.

## Use it on your Windows laptop

You need Windows 10 or 11 (64-bit), a standard user account, and access to PyPI for the other packages (about 400 MB). Run every command below in a **Command Prompt**, not PowerShell.

### 1. Install, once

1. **Check for Python 3.12.** Run `py -0` and look for `3.12` in the list. Python 3.13 also works; 3.14 does not. If it is missing, install Python 3.12 from [python.org](https://www.python.org/downloads/windows/): choose "Install Now" and untick "Use admin privileges when installing py.exe". No administrator rights are needed.

2. **Create the app's own environment.**

   ```bat
   py -3.12 -m venv %LOCALAPPDATA%\eMailSuperSearch\venv
   ```

3. **Install the app from the release.** This fetches both files from GitHub and everything else from PyPI, and takes a few minutes.

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console "emailsupersearch[all] @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.1/emailsupersearch-0.1.1-py3-none-any.whl" "emailsupersearch-models-en @ https://github.com/VikrantKurada/eMailSuperSearch/releases/download/v0.1.1/emailsupersearch_models_en-0.1.0-py3-none-any.whl"
   ```

   If your company network blocks pip from downloading from GitHub, download the two `.whl` files from the [v0.1.1 release page](https://github.com/VikrantKurada/eMailSuperSearch/releases/tag/v0.1.1) in your browser instead, put them in `C:\ess-wheels`, and run:

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m pip install --only-binary=:all: --no-binary red-black-tree-mod,win-unicode-console --find-links C:\ess-wheels "emailsupersearch[all]"
   ```

   If pip times out or reports certificate errors, see the proxy note under Troubleshooting in [docs/install.md](docs/install.md#troubleshooting).

4. **Check the laptop.** The report should end with `Degraded: none`. Lines marked WARN are advice, not failures.

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch doctor
   ```

### 2. Start and use it

1. **Start the app.** Your browser opens on the search page. Keep the Command Prompt window open while you use it; closing it stops the app.

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch
   ```

2. **Create your first project.** Give it a name, add the folders that hold your `.msg` and `.eml` files (type the path or use Browse), and leave OCR switched on. The app only reads those folders and never changes them. The first index of a large archive takes hours, but you can search while it runs.

3. **Search.** Type in the search box and press Enter. Filter by sender, recipients, dates and attachment type on the left. Click a result to read the message, or use "Open in Outlook" or "Reveal in Explorer". "Export to Excel" saves the results, or just the ones you ticked.

4. **Stop the app** when you are done. Wait until it prints "stopped".

   ```bat
   %LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch stop
   ```

To start it with a double-click, save the start command from step 1 in a text file named `Start eMailSuperSearch.bat` on your desktop. Running the start command while the app is already running just opens another browser tab on it.

Your indexes, settings and logs live in `%LOCALAPPDATA%\eMailSuperSearch`. Nothing leaves the laptop.

### 3. Send results or problems back

Open [a new issue](https://github.com/VikrantKurada/eMailSuperSearch/issues/new/choose) and choose **Laptop probe or problem**. Paste the `doctor` output, and for a first run also the output of:

```bat
%LOCALAPPDATA%\eMailSuperSearch\venv\Scripts\python -m emailsupersearch bench --json --vectors 50000
```

**This repository is public, so issues are public.** Never paste email contents, subjects or addresses, and do not name your company, its proxy or its internal tools. The `doctor` report and the `bench` numbers contain only facts about the machine and timings.

### 4. Upgrade to a newer release

Stop the app and wait for "stopped". Then run step 3 of the install again with the file names from the new release's notes, adding `--upgrade` after `install`, and start the app again. Your projects and indexes are kept.

## More

[docs/install.md](docs/install.md) covers the details: where each kind of data lives, an offline bundle for a laptop with no PyPI access, purging, and troubleshooting.

If your company blocks `onnxruntime` (the semantic-search engine), the app still runs with keyword search only and says so. `sqlite-vec`, `tokenizers` and `pdfium` are hard requirements.

## Licence

GPL-3.0-or-later. See `LICENSE`. The English model package is Apache-2.0, and the web interface bundles preact and a Vite helper under the MIT licence; see `ui/public/THIRD_PARTY_NOTICES.txt`.
