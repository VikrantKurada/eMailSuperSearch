import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from emailsupersearch import logging_setup
from emailsupersearch.logging_setup import configure_logging, log_subject, redact


@pytest.fixture(autouse=True, scope="module")
def _reset_logging_state():
    yield
    logger = logging.getLogger("emailsupersearch")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logging_setup._HANDLERS.clear()


def _file_handlers(log, path):
    return [h for h in log.handlers if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path]


def test_redact_hashes_email_addresses_and_paths():
    out = redact(r"sent by jane.doe@example.com from C:\Users\jane\Archive\RE Budget.msg")
    assert "jane.doe@example.com" not in out and "jane" not in out
    assert re.search(r"<addr:[0-9a-f]{8}>", out) and re.search(r"<path:[0-9a-f]{8}>", out)


def test_redact_unc_path(): assert r"\\server" not in redact(r"\\server\share\Q1 Report.pdf")


def test_redact_path_is_case_insensitive():
    out = redact(r"see C:\x\REPORT.PDF now")
    assert "REPORT.PDF" not in out and "<path:" in out


@pytest.mark.parametrize("text, leaked", [
    (r"'C:\\Users\\jane\\Archive\\RE Budget.msg'", "jane"),
    (r"'\\\\server\\share\\Q1 Report.pdf'", "server"),
])
def test_redact_path_with_doubled_backslashes(text, leaked):
    out = redact(text)
    assert leaked not in out and out.count("<path:") == 1


def test_info_handler_redacts_but_debug_does_not(tmp_path):
    log = configure_logging("t", tmp_path, "INFO"); log.info("mail from a@b.com")
    assert "a@b.com" not in (tmp_path / "logs" / "t.log").read_text()
    log2 = configure_logging("t2", tmp_path, "DEBUG"); log2.debug("mail from a@b.com")
    assert "a@b.com" in (tmp_path / "logs" / "t2.log").read_text()


def test_log_subject_hashes_unless_debug(tmp_path):
    configure_logging("emailsupersearch", tmp_path, "INFO"); assert log_subject("RE: Acme") != "RE: Acme"
    configure_logging("emailsupersearch", tmp_path, "DEBUG"); assert log_subject("RE: Acme") == "RE: Acme"


def test_handler_is_rotating_with_spec_limits(tmp_path):
    h = next(h for h in configure_logging("r", tmp_path).handlers if isinstance(h, RotatingFileHandler))
    assert h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 3


def test_path_argument_is_redacted(tmp_path):
    log = configure_logging("patharg", tmp_path, "INFO")
    log.info("open %s", Path(r"C:\Users\jane\Archive\RE Budget.msg"))
    text = (tmp_path / "logs" / "patharg.log").read_text()
    assert "<path:" in text and "jane" not in text


def test_exception_traceback_is_redacted(tmp_path):
    log = configure_logging("traceback", tmp_path, "INFO")
    try:
        with open(r"C:\Users\jane\Archive\nope.msg"):
            pass
    except OSError:
        log.exception("failed")
    text = (tmp_path / "logs" / "traceback.log").read_text()
    assert "Traceback" in text and "<path:" in text and "nope.msg" not in text


def test_split_address_is_redacted(tmp_path):
    log = configure_logging("split", tmp_path, "INFO")
    log.info("mail from %s@%s", "jane", "example.com")
    assert "jane@example.com" not in (tmp_path / "logs" / "split.log").read_text()


def test_malformed_log_call_does_not_raise_or_leak(tmp_path):
    log = configure_logging("malformed", tmp_path, "INFO")
    log.info("to %s and %s", "a@b.com")  # noqa: PLE1206 - deliberately one argument short
    text = (tmp_path / "logs" / "malformed.log").read_text()
    assert "a@b.com" not in text and "<addr:" in text


def test_module_logger_records_reach_file(tmp_path):
    configure_logging("host", tmp_path, "INFO")
    logging.getLogger("emailsupersearch.ingest.x").info("mail from a@b.com")
    text = (tmp_path / "logs" / "host.log").read_text()
    assert "<addr:" in text and "a@b.com" not in text


def test_reconfigure_same_name_keeps_one_handler(tmp_path):
    configure_logging("same", tmp_path, "INFO")
    log = configure_logging("same", tmp_path, "INFO")
    assert len(_file_handlers(log, tmp_path / "logs" / "same.log")) == 1


def test_debug_then_info_redacts_again(tmp_path):
    configure_logging("down", tmp_path, "DEBUG").debug("raw a@b.com")
    log = configure_logging("down", tmp_path, "INFO")
    log.debug("hidden debug line")
    log.info("later a@b.com")
    text = (tmp_path / "logs" / "down.log").read_text()
    assert "raw a@b.com" in text
    assert "later a@b.com" not in text and "later <addr:" in text
    assert "hidden debug line" not in text


def test_info_then_debug_stops_redacting(tmp_path):
    configure_logging("up", tmp_path, "INFO").info("first a@b.com")
    configure_logging("up", tmp_path, "DEBUG").debug("second a@b.com")
    text = (tmp_path / "logs" / "up.log").read_text()
    assert "first a@b.com" not in text and "first <addr:" in text
    assert "second a@b.com" in text


def test_different_root_replaces_handler(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    log = configure_logging("swap", first, "INFO")
    old = _file_handlers(log, first / "logs" / "swap.log")[0]
    log = configure_logging("swap", second, "INFO")
    assert old not in log.handlers and old.stream is None
    assert len(_file_handlers(log, second / "logs" / "swap.log")) == 1
    log.info("written after the move")
    assert "written after the move" in (second / "logs" / "swap.log").read_text()
    assert "written after the move" not in (first / "logs" / "swap.log").read_text()
