import sqlite3

import pytest

from emailsupersearch.db import schema
from emailsupersearch.db.connection import MissingSqliteFeature, connect
from emailsupersearch.db.schema import (
    SCHEMA_VERSION,
    SchemaTooNew,
    migrate,
    schema_version,
)


def test_connect_enables_wal_and_vec(tmp_path):
    c = connect(tmp_path / "p.sqlite"); assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and c.execute("SELECT vec_version()").fetchone()[0]


def test_migrate_creates_all_tables_and_records_version(tmp_path):
    c = connect(tmp_path / "p.sqlite"); assert migrate(c) == SCHEMA_VERSION
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"files","messages","recipients","attachments","chunks","chunks_fts","chunks_trigram","chunks_vec","chains","chain_summaries","jobs","settings","folders"} <= names
    assert schema_version(c) == SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path): c = connect(tmp_path / "p.sqlite"); migrate(c); assert migrate(c) == SCHEMA_VERSION


def test_migrate_refuses_newer_schema(tmp_path):
    c = connect(tmp_path / "p.sqlite"); migrate(c); c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,)); c.commit()
    with pytest.raises(SchemaTooNew): migrate(c)


def test_fts_triggers_keep_index_in_sync(tmp_path):
    c = connect(tmp_path / "p.sqlite"); migrate(c)
    c.execute("INSERT INTO chunks(message_key,part,idx,text,token_count) VALUES('m','body',0,'invoice INV-2024-00017 approved',5)")
    assert c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"INV-2024-00017\"'").fetchone()
    assert c.execute("SELECT rowid FROM chunks_trigram WHERE chunks_trigram MATCH '\"2024-000\"'").fetchone()
    c.execute("DELETE FROM chunks"); assert not c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'invoice'").fetchone()


def test_fts_triggers_follow_text_updates(tmp_path):
    c = connect(tmp_path / "p.sqlite")
    migrate(c)
    c.execute("INSERT INTO chunks(message_key,part,idx,text,token_count) VALUES('m','body',0,'alpha ABC-123',2)")
    c.execute("UPDATE chunks SET text = 'beta XYZ-9' WHERE message_key = 'm'")
    assert not c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'alpha'").fetchone()
    assert c.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"XYZ-9\"'").fetchone()
    assert not c.execute("SELECT rowid FROM chunks_trigram WHERE chunks_trigram MATCH '\"ABC\"'").fetchone()
    assert c.execute("SELECT rowid FROM chunks_trigram WHERE chunks_trigram MATCH '\"XYZ\"'").fetchone()


def test_failed_migration_rolls_back_and_keeps_version(tmp_path, monkeypatch):
    c = connect(tmp_path / "p.sqlite")
    monkeypatch.setattr(schema, "MIGRATIONS", [(1, "CREATE TABLE t1 (a); CREATE TABLE t1 (a);")])
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 1)
    with pytest.raises(sqlite3.OperationalError):
        schema.migrate(c)
    assert schema_version(c) == 0 and not c.in_transaction
    assert not c.execute("SELECT name FROM sqlite_master WHERE name = 't1'").fetchone()


def test_connect_requires_fts5(tmp_path, monkeypatch):
    class NoFts5:
        closed = False

        def execute(self, sql):
            return [("ENABLE_FTS3",)]

        def close(self):
            self.closed = True

    fake = NoFts5()
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: fake)
    with pytest.raises(MissingSqliteFeature, match="fts5"):
        connect(tmp_path / "p.sqlite")
    assert fake.closed
