"""Run one OCR job in a child process, so a crash or a runaway image never takes the host down.

``OcrRunner.run`` starts ``python -m emailsupersearch.ingest.ocr.worker``, hands it the job as one JSON object on
stdin and reads ``{"text": ...}`` (or ``{"error": ...}``) back from stdout; the child is killed after
`HARD_TIMEOUT_S`. The child re-parses the mail file, renders the requested PDF pages (or decodes the image) and
OCRs them until its time budget is spent. Neither side writes the parse cache or the database: the caller hands
the returned text to ``IngestPipeline.attach_ocr_text``.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

from emailsupersearch.ingest.attachments import MAX_OCR_PAGES, render_pdf_page_png
from emailsupersearch.ingest.ocr.base import OcrUnavailable, select_engine
from emailsupersearch.ingest.parse import parse_file
from emailsupersearch.logging_setup import redact
from emailsupersearch.types import ParsedAttachment

log = logging.getLogger(__name__)

TIME_BUDGET_S = 600.0  # the child stops starting new pages after this
HARD_TIMEOUT_S = 660  # the budget plus a minute for start-up, parsing and the last page; then the child is killed
_WORKER_MODULE = "emailsupersearch.ingest.ocr.worker"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class OcrJobFailed(Exception):
    """The OCR child failed, timed out or answered nonsense; the message holds no path."""


def _attachment(attachments: list[ParsedAttachment], ordinal: int) -> ParsedAttachment:
    for att in attachments:
        if att.ordinal == ordinal:
            return att
    raise LookupError(f"the message has no attachment {ordinal}")


def _page_pngs(att: ParsedAttachment, pages: list[int], max_pages: int) -> Iterator[bytes]:
    """PNG bytes of each page to read, rendered one at a time as they are asked for."""
    if att.data is None:
        raise ValueError(f"attachment {att.ordinal} has no bytes")
    if att.kind == "pdf":
        for index in pages[:max_pages]:
            yield render_pdf_page_png(att.data, index)
    elif att.kind == "image":
        with Image.open(io.BytesIO(att.data)) as opened:
            image = opened.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, "PNG")
        yield buf.getvalue()
    else:
        raise ValueError(f"attachment kind {att.kind!r} has no pages to OCR")


def run_ocr_job(
    path: str,
    ordinal: int,
    pages: list[int],
    language: str,
    max_pages: int = MAX_OCR_PAGES,
    time_budget_s: float = TIME_BUDGET_S,
) -> str:
    """The OCR text of attachment ``ordinal`` of the mail file at ``path``, pages separated by a blank line.

    A PDF's zero-based ``pages`` are read in order, at most ``max_pages`` of them; an image is one page. No page
    is started once ``time_budget_s`` has passed, so the text can cover only the first pages. Raises
    ``OcrUnavailable`` when no engine can run, ``ParseError`` when the file cannot be parsed.
    """
    message = parse_file(Path(path))
    att = _attachment(message.attachments, ordinal)
    engine = select_engine(language)
    if engine is None:
        raise OcrUnavailable(f"no OCR engine for {language!r}")
    deadline = time.monotonic() + time_budget_s
    texts: list[str] = []
    for done, png in enumerate(_page_pngs(att, pages, max_pages)):
        if time.monotonic() > deadline:
            log.warning("OCR time budget spent after %d pages", done)
            break
        texts.append(engine.recognize(png).strip())
    return "\n\n".join(text for text in texts if text)


def ocr_subprocess_main() -> None:
    """Entry point of the OCR child: one JSON job on stdin, one JSON line on stdout, exit code 0 or 1."""
    from emailsupersearch.env import apply_zero_network_env

    apply_zero_network_env()
    try:
        job = json.loads(sys.stdin.read())
        with contextlib.redirect_stdout(sys.stderr):  # stray prints from a library cannot corrupt the reply
            text = run_ocr_job(job["path"], int(job["ordinal"]), [int(p) for p in job["pages"]], job["language"])
    except Exception as e:  # noqa: BLE001 - every failure becomes the reply's error
        print(json.dumps({"error": redact(f"{type(e).__name__}: {e}")}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps({"text": text}), flush=True)


def _reply(stdout: str) -> dict | None:
    """The child's JSON reply: its last non-empty stdout line, or None when there is none or it is not an object."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        reply = json.loads(lines[-1])
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


class OcrRunner:
    """Runs each OCR job in a fresh child process."""

    def run(self, job_payload: dict, language: str) -> str:
        """The OCR text for a queued job (``path``, ``ordinal``, ``pages``); raises ``OcrJobFailed``."""
        request = json.dumps(
            {
                "path": job_payload["path"],
                "ordinal": job_payload["ordinal"],
                "pages": job_payload["pages"],
                "language": language,
            }
        )
        try:
            done = subprocess.run(
                [sys.executable, "-m", _WORKER_MODULE],
                check=False,
                input=request,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=HARD_TIMEOUT_S,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise OcrJobFailed("timeout") from None
        reply = _reply(done.stdout)
        if done.returncode == 0 and reply is not None and isinstance(reply.get("text"), str):
            return reply["text"]
        log.debug("OCR child exited with code %s; stderr tail: %s", done.returncode, done.stderr[-2000:])
        if reply is not None and "error" in reply:
            raise OcrJobFailed(str(reply["error"]))
        raise OcrJobFailed(f"the OCR worker exited with code {done.returncode} and no reply")


if __name__ == "__main__":
    ocr_subprocess_main()
