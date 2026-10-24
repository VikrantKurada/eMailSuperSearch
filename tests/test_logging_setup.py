import logging
import re
from logging.handlers import RotatingFileHandler

import pytest

from emailsupersearch import logging_setup
from emailsupersearch.logging_setup import configure_logging, log_subject, redact


@pytest.fixture(autouse=True, scope="module")
def _reset_logging_state():
    yield
    for name in ("t", "t2", "r", "emailsupersearch"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)
        logging_setup._HANDLERS.pop(name, None)


def test_redact_hashes_email_addresses_and_paths():
    out = redact(r"sent by jane.doe@example.com from C:\Users\jane\Archive\RE Budget.msg")
    assert "jane.doe@example.com" not in out and "jane" not in out
    assert re.search(r"<addr:[0-9a-f]{8}>", out) and re.search(r"<path:[0-9a-f]{8}>", out)


def test_redact_unc_path(): assert r"\\server" not in redact(r"\\server\share\Q1 Report.pdf")


def test_redact_path_is_case_insensitive():
    out = redact(r"see C:\x\REPORT.PDF now")
    assert "REPORT.PDF" not in out and "<path:" in out


def test_info_handler_redacts_but_debug_does_not(tmp_path):
    log = configure_logging("t", tmp_path, "INFO"); log.info("mail from a@b.com")
    assert "a@b.com" not in (tmp_path / "logs" / "t.log").read_text()
    log2 = configure_logging("t2", tmp_path, "DEBUG"); log2.debug("mail from a@b.com")
    assert "a@b.com" in (tmp_path / "logs" / "t2.log").read_text()


def test_log_subject_hashes_unless_debug(tmp_path):
    configure_logging("emailsupersearch", tmp_path, "INFO"); assert log_subject("RE: Acme") != "RE: Acme"
    configure_logging("emailsupersearch", tmp_path, "DEBUG"); assert log_subject("RE: Acme") == "RE: Acme"


def test_handler_is_rotating_with_spec_limits(tmp_path):
    h = [h for h in configure_logging("r", tmp_path).handlers if isinstance(h, RotatingFileHandler)][0]
    assert h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 3
