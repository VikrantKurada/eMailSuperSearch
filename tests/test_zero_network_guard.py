"""Zero network (spec 17): a full pass in the host process never connects to anything but this machine.

The pass: generate a corpus, create a project, ingest it, search, export to Excel and, where an OCR engine can
run, drain one OCR job. ``socket.socket.connect`` and ``connect_ex`` are replaced for the whole session by a guard
that records every address whose host is not loopback and raises ``AssertionError`` on it. Anything a dependency
swallows is still in the record, so the test asserts the record is empty rather than trusting the exception.

The parse workers and the OCR child are separate processes and do not inherit the patch. The guard covers the host
process, which is where a telemetry call from a dependency would come from.
"""

from __future__ import annotations

import socket

import pytest

from emailsupersearch.catalog import Catalog
from emailsupersearch.diagnostics.corpus import generate
from emailsupersearch.export.excel import export_results
from emailsupersearch.ingest.ocr.base import select_engine
from emailsupersearch.ingest.ocr.worker import OcrRunner
from emailsupersearch.jobs.queue import claim_next
from emailsupersearch.jobs.supervisor import build_project_supervisor
from emailsupersearch.search.hybrid import search
from emailsupersearch.types import SearchFilters

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _host_of(address: object) -> object:
    """The host part of a socket address: ``(host, port)`` and ``(host, port, flow, scope)`` carry one; a Unix
    path is a string and stays as it is."""
    return address[0] if isinstance(address, tuple) and address else address


def _install_guard(monkeypatch: pytest.MonkeyPatch, blocked: list[tuple]) -> None:
    """Replace ``connect`` and ``connect_ex``: loopback addresses go through, any other is recorded in ``blocked``
    and raises ``AssertionError``."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(address: object) -> None:
        host = _host_of(address)
        if host not in _LOOPBACK:
            blocked.append((host, address[1] if isinstance(address, tuple) and len(address) > 1 else None))
            raise AssertionError(f"blocked a connection to {address!r}: the library may only talk to 127.0.0.1")

    def connect(self: socket.socket, address: object) -> None:
        check(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(scope="session")
def no_network():
    """The list of blocked connection attempts, with the guard installed until the session ends."""
    blocked: list[tuple] = []
    monkeypatch = pytest.MonkeyPatch()
    _install_guard(monkeypatch, blocked)
    yield blocked
    monkeypatch.undo()


def test_the_guard_raises_on_a_non_loopback_connect(monkeypatch):
    blocked: list[tuple] = []
    _install_guard(monkeypatch, blocked)
    with socket.socket() as s:
        with pytest.raises(AssertionError, match="192.0.2.1"):
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: the guard raises before anything is sent
        with pytest.raises(AssertionError):
            s.connect_ex(("2001:db8::1", 9, 0, 0))
    assert blocked == [("192.0.2.1", 9), ("2001:db8::1", 9)]


def test_a_full_pass_makes_no_non_loopback_connection(no_network, tmp_path, embedder_or_none, chunker):
    corpus = tmp_path / "corpus"
    generate(corpus, 6, 3)
    catalog = Catalog(tmp_path / "root")
    sup = None
    try:
        project = catalog.create_project("guard", [corpus])
        store = catalog.open_store(project.id)
        sup = build_project_supervisor(project.id, store, embedder_or_none, chunker, tmp_path / "cache", None)
        sup.prepare()
        sup.run_once()

        response = search(store, embedder_or_none, SearchFilters(query="renewal"))
        assert response.total_messages > 0, "the corpus has 'renewal' threads, so the search must find them"
        cards = [m for chain in response.chains for m in chain.messages] or response.messages
        export_results(cards, tmp_path / "out.xlsx")
        assert (tmp_path / "out.xlsx").is_file()

        if select_engine("en") is not None:
            with store.lock:
                job = claim_next(store.conn, ["ocr"])
            assert job is not None, "the corpus has image attachments, so an OCR job is queued"
            assert isinstance(OcrRunner().run(job.payload, "en"), str)
    finally:
        if sup is not None:
            sup.stop()
        catalog.close()

    assert no_network == []
