"""Rotating per-process log files that never leak addresses, paths or subjects at INFO."""

import hashlib
import logging
import os
import re
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

PATH_RE = re.compile(
    r'(?:[A-Za-z]:|\\\\[^\\\s]+)(?:\\[^\\/:*?"<>|\r\n]+)+\.(?:msg|eml|pdf|docx|xlsx|xlsm|pptx)\b',
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

_HANDLERS: dict[str, RotatingFileHandler] = {}


def _token(kind: str, text: str) -> str:
    digest = hashlib.sha1(text.lower().encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"<{kind}:{digest}>"


def redact(text: str) -> str:
    """Replace file paths, then e-mail addresses, by short stable hashes."""
    text = PATH_RE.sub(lambda m: _token("path", m.group(0)), text)
    return EMAIL_RE.sub(lambda m: _token("addr", m.group(0)), text)


class RedactingFilter(logging.Filter):
    """Redact the message and its string arguments of every record; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


_FILTER = RedactingFilter()


def _level_number(level: str) -> int:
    number = logging.getLevelNamesMapping().get(level.upper())
    if number is None:
        raise ValueError(f"unknown log level: {level!r}")
    return number


def configure_logging(process_name: str, root: Path, level: str = "INFO") -> logging.Logger:
    """Log to root/logs/<process_name>.log; INFO and above are redacted, DEBUG keeps full detail."""
    level_number = _level_number(level)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = os.path.abspath(log_dir / f"{process_name}.log")
    logger = logging.getLogger(process_name)

    handler = _HANDLERS.get(process_name)
    if handler is not None and handler.baseFilename != log_path:
        logger.removeHandler(handler)
        handler.close()
        handler = None
    if handler is None:
        handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
        _HANDLERS[process_name] = handler

    logger.setLevel(level_number)
    handler.setLevel(level_number)
    if level_number > logging.DEBUG:
        handler.addFilter(_FILTER)
    else:
        handler.removeFilter(_FILTER)
    return logger


def log_subject(subject: str) -> str:
    """Return the subject at DEBUG, else a short hash of it."""
    if logging.getLogger("emailsupersearch").getEffectiveLevel() <= logging.DEBUG:
        return subject
    return _token("subj", subject)
