"""Rotating per-process log files that never leak addresses, paths or subjects at INFO."""

import hashlib
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

PATH_RE = re.compile(
    r'(?:[A-Za-z]:|\\\\+[^\\\s]+)(?:\\+[^\\/:*?"<>|\r\n]+)+\.(?:msg|eml|pdf|docx|xlsx|xlsm|pptx)\b',
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

LOGGER_NAME = "emailsupersearch"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
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
    """Redact each record after its arguments are merged into the message; never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log call must not raise into the application
            message = f"{record.msg} {record.args!r}"
        record.msg = redact(message)
        record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the whole formatted record, so exception and traceback text is covered too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_FILTER = RedactingFilter()


def _level_number(level: str) -> int:
    number = logging.getLevelNamesMapping().get(level.upper())
    if number is None:
        raise ValueError(f"unknown log level: {level!r}")
    return number


def configure_logging(process_name: str, root: Path, level: str = "INFO") -> logging.Logger:
    """Send the package logger to root/logs/<process_name>.log; INFO and above are redacted, DEBUG is not.

    The handler is attached to the "emailsupersearch" logger, so every module logging through
    logging.getLogger(__name__) under that package reaches it. process_name only names the file.
    """
    level_number = _level_number(level)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = os.path.abspath(log_dir / f"{process_name}.log")
    logger = logging.getLogger(LOGGER_NAME)

    handler = _HANDLERS.get(process_name)
    if handler is not None and handler.baseFilename != log_path:
        logger.removeHandler(handler)
        handler.close()
        handler = None
    if handler is None:
        handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        logger.addHandler(handler)
        _HANDLERS[process_name] = handler

    logger.setLevel(level_number)
    handler.setLevel(level_number)
    if level_number > logging.DEBUG:
        handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
        handler.addFilter(_FILTER)
    else:
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler.removeFilter(_FILTER)
    return logger


def close_logging() -> None:
    """Close and detach every handler of the package logger and forget the per-process ones, so the log files can be
    deleted (Windows will not delete an open file); a later ``configure_logging`` starts afresh."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in _HANDLERS.values():
        handler.close()  # already closed above when attached; closing twice is harmless
    _HANDLERS.clear()


def log_subject(subject: str) -> str:
    """Return the subject at DEBUG, else a short hash of it."""
    if logging.getLogger(LOGGER_NAME).getEffectiveLevel() <= logging.DEBUG:
        return subject
    return _token("subj", subject)
