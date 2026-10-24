"""Schema migrations for a project database (spec 8.2)."""

import sqlite3

_MIGRATION_1 = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE folders (path TEXT PRIMARY KEY, kind TEXT, last_scan_id INTEGER, reachable INTEGER);
CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT, file_id TEXT, message_key TEXT, parse_status TEXT, status_reason TEXT, last_seen_scan_id INTEGER, tombstoned_at INTEGER);
CREATE TABLE messages (message_key TEXT PRIMARY KEY, message_id TEXT, sent_ts INTEGER, received_ts INTEGER, date_source TEXT, subject TEXT, normalized_subject TEXT, sender_name TEXT, sender_addr TEXT, chain_id TEXT, chain_depth INTEGER, body_text TEXT, body_html TEXT, new_text TEXT, has_attachments INTEGER, flags TEXT);
CREATE TABLE recipients (message_key TEXT, kind TEXT, name TEXT, addr TEXT);
CREATE TABLE attachments (message_key TEXT, ordinal INTEGER, filename TEXT, mime TEXT, size INTEGER, kind TEXT, sha256 TEXT, extract_status TEXT, ocr_status TEXT, embedded_partial INTEGER DEFAULT 0, page_count INTEGER, text_chars INTEGER, PRIMARY KEY (message_key, ordinal));
CREATE TABLE chunks (id INTEGER PRIMARY KEY, message_key TEXT, part TEXT, idx INTEGER, text TEXT, token_count INTEGER, embedded INTEGER DEFAULT 0);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize = "unicode61 tokenchars '-_#@.'");
CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER chunks_fts_au AFTER UPDATE OF text ON chunks BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text); INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE chunks_trigram USING fts5(text, content='chunks', content_rowid='id', tokenize='trigram');
CREATE TRIGGER chunks_trigram_ai AFTER INSERT ON chunks BEGIN INSERT INTO chunks_trigram(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER chunks_trigram_ad AFTER DELETE ON chunks BEGIN INSERT INTO chunks_trigram(chunks_trigram, rowid, text) VALUES ('delete', old.id, old.text); END;
CREATE TRIGGER chunks_trigram_au AFTER UPDATE OF text ON chunks BEGIN INSERT INTO chunks_trigram(chunks_trigram, rowid, text) VALUES ('delete', old.id, old.text); INSERT INTO chunks_trigram(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE chunks_vec USING vec0(chunk_rowid INTEGER PRIMARY KEY, embedding float[384]);
CREATE TABLE chains (chain_id TEXT PRIMARY KEY, root_message_key TEXT, subject TEXT, first_ts INTEGER, last_ts INTEGER, message_count INTEGER, participants_json TEXT);
CREATE TABLE chain_summaries (chain_id TEXT, members_hash TEXT, conclusion TEXT, decisions_json TEXT, open_items_json TEXT, model TEXT, created_at INTEGER);
CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, state TEXT, priority INTEGER, attempts INTEGER, last_error TEXT, created_at INTEGER, updated_at INTEGER);
CREATE INDEX idx_messages_sent_ts ON messages (sent_ts);
CREATE INDEX idx_messages_sender_addr ON messages (sender_addr);
CREATE INDEX idx_messages_chain_id ON messages (chain_id);
CREATE INDEX idx_messages_message_id ON messages (message_id);
CREATE INDEX idx_messages_normalized_subject ON messages (normalized_subject);
CREATE INDEX idx_recipients_addr ON recipients (addr);
CREATE INDEX idx_recipients_message_key ON recipients (message_key);
CREATE INDEX idx_chunks_message_key ON chunks (message_key);
CREATE INDEX idx_files_message_key ON files (message_key);
CREATE INDEX idx_files_last_seen_scan_id ON files (last_seen_scan_id);
CREATE INDEX idx_files_file_id ON files (file_id);
CREATE INDEX idx_jobs_state_priority ON jobs (state, priority);
CREATE INDEX idx_attachments_sha256 ON attachments (sha256);
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _MIGRATION_1)]
SCHEMA_VERSION: int = MIGRATIONS[-1][0]


class SchemaTooNew(Exception):
    """The database was written by a newer version of the program."""


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the stored schema version; 0 when the database has never been migrated."""
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to SCHEMA_VERSION, one transaction per migration, and return the version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        conn.commit()
    stored = schema_version(conn)
    if stored > SCHEMA_VERSION:
        raise SchemaTooNew(f"database schema is version {stored}, this program supports up to {SCHEMA_VERSION}")
    for version, script in MIGRATIONS:
        if version <= stored:
            continue
        try:
            conn.executescript(f"BEGIN;\n{script}\nUPDATE schema_version SET version = {int(version)};\nCOMMIT;")
        except BaseException:
            conn.rollback()
            raise
    return schema_version(conn)
