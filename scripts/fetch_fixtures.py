"""Download extract-msg's own sample ``.msg`` files into ``tests/fixtures/msg/`` (developer machine only).

Lists ``example-msg-files`` in TeamMsgExtractor/msg-extractor at tag ``v0.56.1`` through the GitHub
contents API; if the tag is unknown (404) the same listing is taken from ``master``. Files that are
already present are skipped. The target directory is git-ignored. Standard library only.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com/repos/TeamMsgExtractor/msg-extractor/contents/example-msg-files"
REFS = ("v0.56.1", "master")
TARGET_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "msg"
_HEADERS = {"User-Agent": "emailsupersearch-fetch-fixtures", "Accept": "application/vnd.github+json"}
_CHUNK = 1 << 20


def _get(url: str) -> urllib.request.addinfourl:
    return urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=60)


def _list_entries() -> tuple[str, list[dict]]:
    """The directory listing and the ref it came from; the tag is preferred, master is the fallback."""
    for index, ref in enumerate(REFS):
        try:
            with _get(f"{API}?ref={ref}") as response:
                return ref, json.load(response)
        except urllib.error.HTTPError as err:
            if err.code != 404 or index == len(REFS) - 1:
                raise
            print(f"ref {ref}: not found (404), trying {REFS[index + 1]}")
    raise AssertionError("unreachable")


def main() -> int:
    ref, entries = _list_entries()
    print(f"listing taken from ref {ref}")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(e["name"] for e in entries if e.get("type") == "file" and e["name"].lower().endswith(".msg"))
    by_name = {e["name"]: e for e in entries}
    for name in names:
        dest = TARGET_DIR / name
        if dest.exists():
            print(f"{name}: present, skipping")
            continue
        print(f"{name}: downloading")
        tmp = dest.with_name(dest.name + ".part")
        with _get(by_name[name]["download_url"]) as response, tmp.open("wb") as out:
            while chunk := response.read(_CHUNK):
                out.write(chunk)
        tmp.replace(dest)
    print(f"{len(names)} .msg files in {TARGET_DIR}:")
    for name in names:
        print(f"  {name} ({(TARGET_DIR / name).stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
