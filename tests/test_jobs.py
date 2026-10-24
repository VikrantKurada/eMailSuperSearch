import threading

from emailsupersearch.db.connection import connect
from emailsupersearch.jobs.queue import (
    claim_next,
    complete,
    enqueue,
    fail,
    pending_counts,
    requeue_running,
)


def test_enqueue_claim_complete(conn):
    id1 = enqueue(conn, "ocr", {"a": 1}, 50); id2 = enqueue(conn, "ocr", {"a": 2}, 10)
    assert claim_next(conn, ["ocr"]).id == id2; complete(conn, id2); assert claim_next(conn, ["ocr"]).id == id1


def test_fail_retries_three_times_then_fails(conn):
    j = enqueue(conn, "ocr", {}, 1); [fail(conn, j, "x", retry=True) for _ in range(3)]; assert pending_counts(conn)["failed"] == 1


def test_claim_next_is_atomic_across_threads(conn, tmp_path):
    ids = {enqueue(conn, "ocr", {"n": n}) for n in range(20)}
    claimed: list[list[int]] = [[], []]
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def drain(slot: int) -> None:
        c = connect(tmp_path / "q.sqlite")
        try:
            start.wait()
            while (job := claim_next(c, ["ocr"])) is not None:
                claimed[slot].append(job.id)
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=drain, args=(slot,)) for slot in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert not set(claimed[0]) & set(claimed[1])
    assert sorted(claimed[0] + claimed[1]) == sorted(ids)
    assert pending_counts(conn) == {"queued": 0, "running": 20, "done": 0, "failed": 0}


def test_requeue_running(conn):
    j1 = enqueue(conn, "ocr", {}); j2 = enqueue(conn, "ocr", {}); claim_next(conn, ["ocr"]); claim_next(conn, ["ocr"]); complete(conn, j2)
    assert requeue_running(conn) == 1 and pending_counts(conn) == {"queued": 1, "running": 0, "done": 1, "failed": 0}
    job = claim_next(conn, ["ocr"]); assert job.id == j1 and job.state == "running" and job.attempts == 0
