"""The ingest pipeline: one mail file in, stored rows out.

``ingest_file`` turns a file the reconcile scan queued into rows: the file is hashed and parsed (through the shared
parse cache and the worker pool), identified by its message key, given a chain, stripped of quoted history and
stored with its attachments and chunks; the chunks are embedded, and attachments with pages to read are queued for
OCR. ``apply_scan`` runs it over one scan result, ``remove_path`` handles a purged file and ``attach_ocr_text``
brings in an OCR result.

The supervisor calls every method from its single worker thread. Chain assignment reads the stored messages before
one is written (``assign_chain``, then ``upsert_message``, then ``update_chain_row``), and that one thread is what
keeps the chain bookkeeping consistent: the pipeline adds no lock around those steps beyond the store's own.

Every file row the pipeline handles leaves ``pending``, as ``indexed``, ``indexed_partial`` or ``failed`` (a
duplicate export is ``indexed`` with the reason ``duplicate``), and the row is written only through
``set_file_parse_result``. A failure never costs an indexed message: a path that already held one keeps the link,
and the message, its chunks and vectors stay as they were. A message is kept only while a live file links to it:
when a path stops holding a message (it now holds another one or duplicates another live path, or it was purged)
and no other live path links that message, the message goes, with its chunks and vectors.

Two guards keep hostile input out of the index. A chunk's text is cut at ``CHUNK_TEXT_MAX_CHARS`` before it is
stored, keyword-indexed or embedded: a megabyte with no whitespace is one "word" the chunker cannot split. And an
embedding that is not finite is never stored; its chunk stays keyword-only (``embedded = 0``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np

from emailsupersearch.index.chains import assign_chain, update_chain_row
from emailsupersearch.ingest.chunker import (
    MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT,
    Chunker,
    ChunkText,
    context_line_for,
)
from emailsupersearch.ingest.hygiene import new_text
from emailsupersearch.ingest.identity import message_key
from emailsupersearch.ingest.parse import ParseError
from emailsupersearch.ingest.parse_cache import ParseCache, append_ocr_text, sha256_file
from emailsupersearch.ingest.workers import ParseMemoryExceeded, ParsePool, ParseTimeout
from emailsupersearch.logging_setup import log_subject
from emailsupersearch.types import AttachmentText, ParsedDocument, ParsedMessage

if TYPE_CHECKING:
    from emailsupersearch.index.embedder import OnnxEmbedder
    from emailsupersearch.index.vectors import VectorIndex
    from emailsupersearch.ingest.scan import ScanResult
    from emailsupersearch.store import ProjectStore

log = logging.getLogger(__name__)

# Longest chunk text stored, keyword-indexed or embedded; a longer chunk is cut and its file row says so.
CHUNK_TEXT_MAX_CHARS = 10_000
CHUNK_TRUNCATED_NOTE = "chunk text truncated"

_EMBED_BATCH = 32
_REASON_MAX_CHARS = 200
_NOT_INDEXED = ("unsupported", "failed")  # attachment statuses that make the file ``indexed_partial``

_EARLIER_IN_CHAIN = "SELECT 1 FROM messages WHERE chain_id = ? AND message_key != ? AND sent_ts < ? LIMIT 1"
_OTHER_IN_CHAIN = "SELECT 1 FROM messages WHERE chain_id = ? AND message_key != ? LIMIT 1"
_ATTACHMENT_FILENAME = "SELECT filename FROM attachments WHERE message_key = ? AND ordinal = ?"

IngestStatus = Literal["indexed", "indexed_partial", "failed", "skipped_duplicate"]


@dataclass
class IngestOutcome:
    """What ``ingest_file`` made of one file; ``chunks`` counts the chunks stored, ``ocr_jobs`` the jobs queued."""

    path: str
    message_key: str | None
    status: IngestStatus
    reason: str = ""
    chunks: int = 0
    ocr_jobs: int = 0


@dataclass
class _Part:
    """One part of a message on its way into the store: ``body`` or ``att:<ordinal>``."""

    name: str
    ordinal: int | None  # the attachment's ordinal; None for the body
    chunks: list[ChunkText]
    truncated: bool = False  # a chunk's text was cut at CHUNK_TEXT_MAX_CHARS
    rowids: list[int] = field(default_factory=list)


def _timeout_reason(e: ParseTimeout) -> str:
    """``timeout`` when the worker ran out of time, else the pool's own message (``pool closed``, ``worker died: ...``).

    The message is ``e.reason``: ``str(e)`` puts the path in front, which a folder named "timeouts" would misread.
    """
    msg = e.reason
    return "timeout" if (not msg or "timeout" in msg.lower() or "timed out" in msg.lower()) else msg[:_REASON_MAX_CHARS]


class IngestPipeline:
    """Indexes mail files into one project's store. Not thread-safe: one supervisor thread drives it."""

    def __init__(
        self,
        store: ProjectStore,
        cache: ParseCache,
        pool: ParsePool,
        chunker: Chunker,
        embedder: OnnxEmbedder | None,
        vectors: VectorIndex | None,
        enqueue_ocr: Callable[[dict], int] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.cache = cache
        self.pool = pool
        self.chunker = chunker
        self.embedder = embedder
        self.vectors = vectors
        self.enqueue_ocr = enqueue_ocr
        self.on_event = on_event

    # ---- public API ----

    def ingest_file(self, path: Path, scan_id: int) -> IngestOutcome:
        """Index one mail file and record the result on its manifest row; emits a ``file_indexed`` event.

        It expects the manifest row the reconcile scan created for ``path``: without one the message is indexed but
        nothing is recorded for the file. Never raises: any failure is recorded as ``failed`` with its reason, and a
        message the path already held stays linked and untouched. ``scan_id`` (the scan that queued the file)
        appears only in DEBUG logs.
        """
        path = Path(path)
        outcome = self._ingest(path)
        log.debug("scan %d: %s %s", scan_id, outcome.status, path)
        self._emit(
            {"type": "file_indexed", "path": outcome.path, "status": outcome.status, "message_key": outcome.message_key}
        )
        return outcome

    def remove_path(self, path: str, message_key: str | None = None) -> str | None:
        """Unlink ``path`` from its message, deleting its manifest row if there is one.

        The message key comes from the row, or from ``message_key`` when the scan already purged the row. When no
        live path links the message any more, the message goes with its chunks and vectors and its chain row is
        recomputed. Returns the key the path was linked to, or None when it held no message.
        """
        rec = self.store.get_file(path)
        key = message_key
        if rec is not None:
            key = rec.message_key
            self.store.delete_file(path)
        if key is None:
            return None
        self._drop_if_orphaned(key)
        return key

    def apply_scan(self, result: ScanResult) -> list[IngestOutcome]:
        """Act on one scan: announce its renames (the scan already moved those rows), parse ``to_parse`` and
        remove what it purged."""
        for old, new in result.renamed:
            self._emit({"type": "renamed", "old": old, "new": new})
        outcomes = [self.ingest_file(path, result.scan_id) for path in result.to_parse]
        for rec in result.purged:
            self.remove_path(rec.path, rec.message_key)
        return outcomes

    def attach_ocr_text(self, sha256: str, ordinal: int, text: str) -> None:
        """Bring in the OCR text of attachment ``ordinal`` of the file whose bytes hash to ``sha256``.

        OCR reads only the pages with no text layer, so its text is appended to the attachment's text layer
        (``append_ocr_text``). The parse cache is updated first, so a re-index keeps the combined text. Then, for
        every stored message a live file with those bytes links to, the part ``att:<ordinal>`` is re-chunked and
        re-embedded from the combined text and the attachment is marked ``ok``. Raises ``KeyError`` when the cache
        has no such entry or attachment.
        """
        doc = self.cache.get(sha256)
        existing = next((a.text for a in doc.attachments if a.ordinal == ordinal), "") if doc is not None else ""
        self.cache.set_ocr_text(sha256, ordinal, text)
        combined = append_ocr_text(existing, text)
        for key in self.store.message_keys_for_sha256(sha256):
            self._attach_ocr_part(key, ordinal, combined)

    # ---- stages ----

    def _ingest(self, path: Path) -> IngestOutcome:
        where = str(path)
        key: str | None = None
        doc: ParsedDocument | None = None
        try:
            sha, doc = self._parse(path)
            key = message_key(doc.message)
            if self._is_duplicate(where, key):
                self._record(where, key, "indexed", "duplicate")
                log.info("duplicate export of a stored message: %s", log_subject(doc.message.subject))
                return IngestOutcome(where, key, "skipped_duplicate", "duplicate")
            return self._index(path, sha, doc, key)
        except ParseTimeout as e:
            reason, kind = _timeout_reason(e), "timeout"
        except ParseMemoryExceeded:
            reason = kind = "memory_cap"
        except ParseError as e:
            reason, kind = str(e)[:_REASON_MAX_CHARS], "parse_error"
        except Exception as e:
            reason, kind = f"{type(e).__name__}: {e}"[:_REASON_MAX_CHARS], None
            subject = log_subject(doc.message.subject) if doc is not None else "an unparsed file"
            log.warning("indexing failed for %s: %s", subject, type(e).__name__)
            log.debug("indexing failed", exc_info=True)
        if kind is not None:  # the reason can name the path, so WARNING carries only the kind of failure
            log.warning("a file could not be parsed: %s", kind)
            log.debug("parse failed for %s: %s", where, reason)
        return self._fail(where, key, reason)

    def _parse(self, path: Path) -> tuple[str, ParsedDocument]:
        """The file's hash and its parsed document, from the cache when it holds the hash, else from the pool."""
        sha = sha256_file(path)
        doc = self.cache.get(sha)
        if doc is None:
            doc = self.pool.parse(path)
            self.cache.put(doc)
        return sha, doc

    def _is_duplicate(self, where: str, key: str) -> bool:
        """Whether the message is already stored and another live path holds it (an export of the same email)."""
        others = [p for p in self.store.paths_for_message(key) if p != where]
        return bool(others) and self.store.message(key) is not None

    def _index(self, path: Path, sha: str, doc: ParsedDocument, key: str) -> IngestOutcome:
        m = doc.message
        chain_id, previous_chain, text = self._store_message(doc, key)
        parts = self._parts(m, text, doc.attachments)
        self._store_chunks(key, parts)
        self._embed(key, parts)
        update_chain_row(self.store, chain_id)
        if previous_chain is not None and previous_chain != chain_id:
            update_chain_row(self.store, previous_chain)  # a re-ingested message can move to another chain
        ocr_jobs = self._enqueue_ocr(path, sha, doc.attachments)
        return self._finish(str(path), key, m.subject, doc.attachments, parts, ocr_jobs)

    def _store_message(self, doc: ParsedDocument, key: str) -> tuple[str, str | None, str]:
        """Write the message and its attachment rows; returns its chain, the chain it had before, and its new text."""
        m = doc.message
        stored = self.store.message(key)
        previous_chain = stored["chain_id"] if stored is not None else None
        chain_id, depth = assign_chain(self.store, m, key)
        text = new_text(m.body_text, self._has_parent(chain_id, key, m.sent_ts))
        self.store.upsert_message(m, key, text, chain_id, depth)
        self.store.upsert_attachments(key, doc.attachments, "pending" if self.enqueue_ocr is not None else "unavailable")
        return chain_id, previous_chain, text

    def _has_parent(self, chain_id: str, key: str, sent_ts: int | None) -> bool:
        """Whether the chain already holds an earlier message (any other message when this one is undated)."""
        with self.store.lock:
            if sent_ts is None:
                row = self.store.conn.execute(_OTHER_IN_CHAIN, (chain_id, key)).fetchone()
            else:
                row = self.store.conn.execute(_EARLIER_IN_CHAIN, (chain_id, key, sent_ts)).fetchone()
        return row is not None

    def _parts(self, m: ParsedMessage, text: str, attachments: list[AttachmentText]) -> list[_Part]:
        """The body and every attachment with text, chunked under their context lines."""
        sender = m.sender_name or m.sender_addr
        parts = [self._part("body", None, text, context_line_for(sender, m.sent_ts, m.subject, "body"))]
        for att in attachments:
            if att.text:
                line = context_line_for(sender, m.sent_ts, m.subject, att.filename)
                parts.append(self._part(f"att:{att.ordinal}", att.ordinal, att.text, line))
        return parts

    def _part(self, name: str, ordinal: int | None, text: str, context_line: str) -> _Part:
        """Chunk one part, cutting every chunk's text at ``CHUNK_TEXT_MAX_CHARS``."""
        part = _Part(name, ordinal, [])
        for c in self.chunker.chunk(text, context_line):
            if len(c.text) > CHUNK_TEXT_MAX_CHARS:
                c = ChunkText(c.idx, c.text[:CHUNK_TEXT_MAX_CHARS], c.token_count)
                part.truncated = True
            part.chunks.append(c)
        return part

    def _store_chunks(self, key: str, parts: list[_Part]) -> None:
        """Replace the message's chunks part by part; parts it no longer has (a removed attachment) are cleared."""
        current = {p.name for p in parts}
        for stale in self.store.chunk_parts(key):
            if stale not in current:
                self.store.replace_chunks(key, stale, [])
        for part in parts:
            part.rowids = self.store.replace_chunks(key, part.name, part.chunks)

    def _embed(self, key: str, parts: list[_Part]) -> None:
        """Embed every body chunk and the first ``MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT`` chunks of each attachment.

        An attachment with more chunks keeps them keyword-only and is marked ``embedded_partial``.
        """
        if self.embedder is None or self.vectors is None:
            return
        rowids: list[int] = []
        texts: list[str] = []
        for part in parts:
            take = len(part.chunks) if part.ordinal is None else MAX_EMBEDDED_CHUNKS_PER_ATTACHMENT
            rowids += part.rowids[:take]
            texts += [c.text for c in part.chunks[:take]]
            if part.ordinal is not None and len(part.chunks) > take:
                self.store.set_attachment_status(key, part.ordinal, embedded_partial=True)
        for start in range(0, len(rowids), _EMBED_BATCH):
            self._embed_batch(rowids[start : start + _EMBED_BATCH], texts[start : start + _EMBED_BATCH])

    def _embed_batch(self, rowids: list[int], texts: list[str]) -> None:
        """Embed and store one batch; a row whose vector is not finite is skipped and stays ``embedded = 0``."""
        rows: list[tuple[int, np.ndarray]] = []
        for rowid, vector in zip(rowids, self.embedder.embed(texts, batch_size=_EMBED_BATCH), strict=True):
            if np.isfinite(vector).all():
                rows.append((rowid, vector))
            else:
                log.debug("chunk %d has a non-finite embedding; it stays keyword-only", rowid)
        if rows:
            with self.store.lock:  # the vector index writes through the store's writer connection
                self.vectors.upsert(rows)
                self.store.mark_embedded([rowid for rowid, _ in rows])

    def _enqueue_ocr(self, path: Path, sha: str, attachments: list[AttachmentText]) -> int:
        """Queue one OCR job per attachment with pages to read; returns how many were queued."""
        if self.enqueue_ocr is None:
            return 0
        queued = 0
        for att in attachments:
            if att.status == "needs_ocr" and att.needs_ocr_pages:
                job = {"path": str(path), "sha256": sha, "ordinal": att.ordinal, "pages": list(att.needs_ocr_pages)}
                self.enqueue_ocr(job)
                queued += 1
        return queued

    def _finish(
        self, where: str, key: str, subject: str, attachments: list[AttachmentText], parts: list[_Part], ocr_jobs: int
    ) -> IngestOutcome:
        """Record the file as ``indexed``, or ``indexed_partial`` when an attachment could not be read."""
        skipped = sum(1 for att in attachments if att.status in _NOT_INDEXED)
        status: IngestStatus = "indexed_partial" if skipped else "indexed"
        notes = [f"{skipped} attachments not indexed"] if skipped else []
        if any(p.truncated for p in parts):
            notes.append(CHUNK_TRUNCATED_NOTE)
        reason = "; ".join(notes)
        self._record(where, key, status, reason)
        chunks = sum(len(p.chunks) for p in parts)
        log.info("%s %s: %d chunks, %d OCR jobs", status, log_subject(subject), chunks, ocr_jobs)
        return IngestOutcome(where, key, status, reason, chunks, ocr_jobs)

    # ---- links between files and messages ----

    def _record(self, where: str, key: str | None, status: str, reason: str) -> None:
        """Write the parse result on the file row; the message the path held before goes if nothing else holds it."""
        rec = self.store.get_file(where)
        self.store.set_file_parse_result(where, key, status, reason)
        if rec is not None and rec.message_key is not None and rec.message_key != key:
            self._drop_if_orphaned(rec.message_key)

    def _fail(self, where: str, key: str | None, reason: str) -> IngestOutcome:
        """Record a failed file. A message the path already held keeps its link and stays as it was; a message
        this attempt wrote under another key goes when no live path links it."""
        kept: str | None = None
        try:
            rec = self.store.get_file(where)
            kept = rec.message_key if rec is not None else None
            self.store.set_file_parse_result(where, kept, "failed", reason)
            if key is not None and key != kept:
                self._drop_if_orphaned(key)
        except Exception as e:
            log.warning("could not record a failed file: %s", type(e).__name__)
            log.debug("recording the failure of %s failed", where, exc_info=True)
        return IngestOutcome(where, kept if kept is not None else key, "failed", reason)

    def _drop_if_orphaned(self, key: str) -> None:
        """Delete the message (chunks and vectors too) when no live path links it, then recompute its chain row."""
        if self.store.paths_for_message(key):
            return
        stored = self.store.message(key)
        self.store.delete_message(key)
        if stored is not None and stored["chain_id"] is not None:
            update_chain_row(self.store, stored["chain_id"])

    # ---- OCR results and events ----

    def _attach_ocr_part(self, key: str, ordinal: int, text: str) -> None:
        stored = self.store.message(key)
        with self.store.lock:
            att = self.store.conn.execute(_ATTACHMENT_FILENAME, (key, ordinal)).fetchone()
        if stored is None or att is None:
            log.debug("no stored message %s with attachment %d for an OCR result", key, ordinal)
            return
        sender = stored["sender_name"] or stored["sender_addr"] or ""
        line = context_line_for(sender, stored["sent_ts"], stored["subject"] or "", att["filename"] or "")
        part = self._part(f"att:{ordinal}", ordinal, text, line)
        part.rowids = self.store.replace_chunks(key, part.name, part.chunks)
        self._embed(key, [part])
        self.store.set_attachment_status(key, ordinal, extract_status="ok", ocr_status="ok", text_chars=len(text))

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception as e:
            log.warning("an event callback failed: %s", type(e).__name__)
            log.debug("event callback failed for %r", event, exc_info=True)
