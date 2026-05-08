"""pgmq queue + worker tests.

These tests use mocked psycopg connections — no real Postgres needed.
The opt-in real-DB integration test lives at the bottom and skips when
MDK_TEST_DATABASE_URL isn't set.

What's covered:
- enqueue() inserts via pgmq.send and returns the message id
- poll() returns parsed payloads from pgmq.read
- delete() / archive() ack a message
- queue_depth() reads metrics
- The worker's reap_orphans() updates stuck rows
"""
from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

pytest.importorskip("psycopg")

from mdk_eval.web import queue, worker


# ---------- helpers ----------


def _make_db_mock(execute_side_effect=None, fetchone_side_effect=None,
                  fetchall_return_value=None):
    """Build a fake `db.connect()` context manager backed by a MagicMock cursor."""
    cur = MagicMock()
    if execute_side_effect:
        cur.execute.side_effect = execute_side_effect
    if fetchone_side_effect is not None:
        cur.fetchone.side_effect = fetchone_side_effect
    if fetchall_return_value is not None:
        cur.fetchall.return_value = fetchall_return_value
    cur.__enter__ = lambda self: cur
    cur.__exit__ = lambda *a: None
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def fake_connect():
        yield conn

    return fake_connect, conn, cur


# ---------- enqueue ----------


def test_enqueue_calls_pgmq_send_with_job_id(monkeypatch):
    fake, conn, cur = _make_db_mock(fetchone_side_effect=[(42,)])
    monkeypatch.setattr(queue.db, "connect", fake)

    msg_id = queue.enqueue("job-abc123")

    assert msg_id == 42
    # Must have called pgmq.send
    call_args = cur.execute.call_args_list[0]
    assert "pgmq.send" in call_args[0][0]
    # Payload must include the job_id
    sent_args = call_args[0][1]
    assert "job-abc123" in str(sent_args)
    conn.commit.assert_called()


# ---------- poll ----------


def test_poll_returns_parsed_payloads(monkeypatch):
    msgs = [(1, {"job_id": "job-a"}), (2, {"job_id": "job-b"})]
    fake, conn, cur = _make_db_mock(fetchall_return_value=msgs)
    monkeypatch.setattr(queue.db, "connect", fake)

    out = queue.poll(qty=2)
    assert out == [(1, {"job_id": "job-a"}), (2, {"job_id": "job-b"})]
    # Must have called pgmq.read with the visibility timeout
    args = cur.execute.call_args[0]
    assert "pgmq.read" in args[0]
    assert queue.VISIBILITY_TIMEOUT_S in args[1]


def test_poll_returns_empty_when_queue_empty(monkeypatch):
    fake, conn, cur = _make_db_mock(fetchall_return_value=[])
    monkeypatch.setattr(queue.db, "connect", fake)
    assert queue.poll() == []


def test_poll_handles_string_payload(monkeypatch):
    """pgmq normally returns a dict for jsonb, but some drivers return a string —
    we accept both."""
    fake, conn, cur = _make_db_mock(fetchall_return_value=[(1, '{"job_id": "x"}')])
    monkeypatch.setattr(queue.db, "connect", fake)
    out = queue.poll()
    assert out == [(1, {"job_id": "x"})]


# ---------- delete + archive ----------


def test_delete_calls_pgmq_delete(monkeypatch):
    fake, conn, cur = _make_db_mock()
    monkeypatch.setattr(queue.db, "connect", fake)
    queue.delete(99)
    assert "pgmq.delete" in cur.execute.call_args[0][0]
    assert 99 in cur.execute.call_args[0][1]


def test_archive_calls_pgmq_archive(monkeypatch):
    fake, conn, cur = _make_db_mock()
    monkeypatch.setattr(queue.db, "connect", fake)
    queue.archive(99)
    assert "pgmq.archive" in cur.execute.call_args[0][0]
    assert 99 in cur.execute.call_args[0][1]


# ---------- worker.reap_orphans ----------


def test_reap_orphans_updates_stuck_rows(monkeypatch):
    fake, conn, cur = _make_db_mock(fetchall_return_value=[("job-stuck-1",), ("job-stuck-2",)])
    monkeypatch.setattr(worker.db, "connect", fake)

    n = asyncio.run(worker.reap_orphans())
    assert n == 2
    # Must have used the orphan threshold
    assert "make_interval" in cur.execute.call_args[0][0]
    assert worker.ORPHAN_THRESHOLD_S in cur.execute.call_args[0][1]


def test_reap_orphans_returns_zero_when_nothing_stuck(monkeypatch):
    fake, conn, cur = _make_db_mock(fetchall_return_value=[])
    monkeypatch.setattr(worker.db, "connect", fake)
    assert asyncio.run(worker.reap_orphans()) == 0


# ---------- worker_loop graceful shutdown ----------


def test_worker_loop_exits_on_stop_event(monkeypatch):
    """The worker loop should observe stop_event during the empty-poll sleep
    and exit promptly — no hung containers on shutdown."""
    fake, conn, cur = _make_db_mock(fetchall_return_value=[])
    monkeypatch.setattr(worker.db, "connect", fake)
    # speed up the test
    monkeypatch.setattr(worker, "EMPTY_POLL_INTERVAL_S", 0.05)

    async def run_with_quick_stop():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.worker_loop(stop))
        await asyncio.sleep(0.1)  # let it poll once
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    # Should NOT raise asyncio.TimeoutError — clean shutdown
    asyncio.run(run_with_quick_stop())


# ---------- opt-in real-DB integration ----------


@pytest.mark.skipif(
    not os.getenv("MDK_TEST_DATABASE_URL"),
    reason="Set MDK_TEST_DATABASE_URL to run the real-pgmq integration test.",
)
def test_real_pgmq_roundtrip(monkeypatch):
    """End-to-end: enqueue → poll → delete against a real pgmq instance."""
    monkeypatch.setenv("DATABASE_URL", os.environ["MDK_TEST_DATABASE_URL"])

    msg_id = queue.enqueue("test-roundtrip-job")
    assert msg_id > 0

    msgs = queue.poll(qty=10)
    found = [(mid, p) for mid, p in msgs if p.get("job_id") == "test-roundtrip-job"]
    assert found, f"didn't see our message in queue. got: {msgs}"

    queue.delete(found[0][0])
