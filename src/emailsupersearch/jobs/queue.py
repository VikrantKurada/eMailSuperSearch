"""A durable job queue in the project database's ``jobs`` table.

A job moves ``queued`` -> ``running`` -> ``done``, or to ``failed`` once it has failed `MAX_ATTEMPTS` times (or
once without a retry). Jobs survive a restart: the supervisor calls ``requeue_running`` on start, so a job that
was running when the process died is claimed again.

Every function takes a bare ``sqlite3.Connection`` and each write runs in its own transaction (``with conn:``).
A caller that shares a ``ProjectStore``'s writer connection holds ``store.lock`` around these calls, as the
store's own methods do. ``claim_next`` is a single ``UPDATE ... RETURNING`` statement, so two connections
claiming at once never get the same job.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STATES = (QUEUED, RUNNING, DONE, FAILED)
MAX_ATTEMPTS = 3  # the third failure is final
MAX_ERROR_CHARS = 500


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    state: str
    priority: int
    attempts: int
    last_error: str | None


def _now() -> int:
    return int(time.time())


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict, priority: int = 100) -> int:
    """Queue a job and return its id; a lower ``priority`` number is claimed first."""
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, payload, state, priority, attempts, last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
            (kind, json.dumps(payload), QUEUED, priority, now, now),
        )
    return cur.lastrowid


def claim_next(conn: sqlite3.Connection, kinds: list[str]) -> Job | None:
    """Mark the next queued job of one of ``kinds`` running and return it (lowest priority number, then id)."""
    if not kinds:
        return None
    marks = ", ".join("?" * len(kinds))
    with conn:
        rows = conn.execute(
            f"UPDATE jobs SET state = ?, updated_at = ? WHERE id = ("
            f"SELECT id FROM jobs WHERE state = ? AND kind IN ({marks}) ORDER BY priority, id LIMIT 1"
            ") RETURNING id, kind, payload, state, priority, attempts, last_error",
            (RUNNING, _now(), QUEUED, *kinds),
        ).fetchall()
    if not rows:
        return None
    job_id, kind, payload, state, priority, attempts, last_error = rows[0]
    return Job(job_id, kind, json.loads(payload), state, priority, attempts, last_error)


def complete(conn: sqlite3.Connection, job_id: int) -> None:
    """Mark a job done."""
    with conn:
        conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?", (DONE, _now(), job_id))


def fail(conn: sqlite3.Connection, job_id: int, error: str, retry: bool) -> None:
    """Record a failure: re-queue the job when ``retry`` and it has failed fewer than `MAX_ATTEMPTS` times, else fail it."""
    with conn:
        conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, updated_at = ?, "
            "state = CASE WHEN ? AND attempts + 1 < ? THEN ? ELSE ? END WHERE id = ?",
            (error[:MAX_ERROR_CHARS], _now(), bool(retry), MAX_ATTEMPTS, QUEUED, FAILED, job_id),
        )


def pending_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The number of jobs in each state; every state is a key, 0 when no job is in it."""
    counts = dict.fromkeys(STATES, 0)
    for state, count in conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state"):
        counts[state] = count
    return counts


def requeue_running(conn: sqlite3.Connection) -> int:
    """Put every running job back in the queue (crash recovery on start); returns how many moved."""
    with conn:
        cur = conn.execute("UPDATE jobs SET state = ?, updated_at = ? WHERE state = ?", (QUEUED, _now(), RUNNING))
    return cur.rowcount


def purge_done(conn: sqlite3.Connection, older_than_s: int) -> int:
    """Delete done jobs last updated more than ``older_than_s`` seconds ago; returns how many were deleted."""
    with conn:
        cur = conn.execute("DELETE FROM jobs WHERE state = ? AND updated_at < ?", (DONE, _now() - older_than_s))
    return cur.rowcount
