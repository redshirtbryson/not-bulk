"""Unit tests for jobqueue payload validation, backoff math, and NOTIFY.

The claim/complete/fail/reclaim SQL is exercised against the REAL local
Postgres in test_jobqueue_integration below (skipped unless DATABASE_URL is
set). These pure tests use FakePool so they run everywhere.
"""
from __future__ import annotations

import json
import os

import pytest

from notbulk import jobqueue
from tests.fakes import FakePool


# ---- validate_payload ----------------------------------------------------

def test_validate_payload_detect_ok():
    p = {"photo_id": "018f-uuid"}
    assert jobqueue.validate_payload("detect", p) == p


def test_validate_payload_identify_ok():
    p = {"card_id": "018f-uuid"}
    assert jobqueue.validate_payload("identify", p) == p


def test_validate_payload_fetch_source_ok():
    p = {"photo_id": "018f-uuid"}
    assert jobqueue.validate_payload("fetch_source", p) == p


def test_validate_payload_ingest_correction_ok():
    p = {"card_id": "018f-uuid", "actual_ref_id": "sv4-123", "predicted_ref_id": "sv4-120"}
    assert jobqueue.validate_payload("ingest_correction", p) == p


def test_validate_payload_ingest_correction_null_predicted_ref_id_ok():
    """predicted_ref_id must be present but may be None (Assembly Resolution 5 / fix F5)."""
    p = {"card_id": "018f-uuid", "actual_ref_id": "sv4-123", "predicted_ref_id": None}
    assert jobqueue.validate_payload("ingest_correction", p) == p


def test_validate_payload_missing_key_raises():
    with pytest.raises(ValueError, match="missing key 'photo_id'"):
        jobqueue.validate_payload("detect", {})


def test_validate_payload_extra_key_raises():
    with pytest.raises(ValueError, match="unexpected key 'evil'"):
        jobqueue.validate_payload("detect", {"photo_id": "x", "evil": 1})


def test_validate_payload_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown job type 'nope'"):
        jobqueue.validate_payload("nope", {})


def test_validate_payload_non_string_value_raises():
    with pytest.raises(ValueError, match="'photo_id' must be a string"):
        jobqueue.validate_payload("detect", {"photo_id": 5})


# ---- backoff math --------------------------------------------------------

def test_backoff_seconds_grows_with_attempts():
    assert jobqueue.backoff_seconds(1) == 30
    assert jobqueue.backoff_seconds(2) == 60
    assert jobqueue.backoff_seconds(3) == 90


# ---- fail() requeue vs dead (SQL shape assertions via FakePool) ----------

def test_fail_requeue_uses_backoff_interval():
    pool = FakePool([[("queued",)]])   # RETURNING status -> requeued
    status = jobqueue.fail(pool, "job-1", "boom", dead=False)
    assert status == "queued"
    sql, params = pool.cursor.executed[0]
    nospace = sql.lower().replace(" ", "")
    # Self-terminating requeue: 'queued' branch + max_attempts promotion + backoff.
    assert "else'queued'end" in nospace
    assert "attempts>=max_attemptsthen'failed'" in nospace
    assert "make_interval(secs=>%s*attempts)" in nospace
    assert "returningstatus" in nospace
    assert "job-1" in params
    assert 30 in params            # the 30s backoff step is bound


def test_fail_dead_marks_failed():
    pool = FakePool([[("failed",)]])   # RETURNING status -> failed
    status = jobqueue.fail(pool, "job-2", "fatal", dead=True)
    assert status == "failed"
    sql, params = pool.cursor.executed[0]
    low = sql.lower().replace(" ", "")
    assert "status='failed'" in low
    assert "returningstatus" in low
    assert "fatal" in params


# ---- notify_progress -----------------------------------------------------

def test_notify_progress_builds_pg_notify_json_payload():
    pool = FakePool([[]])
    jobqueue.notify_progress(pool, "batch-1", "card_identified", card_id="card-9")
    sql, params = pool.cursor.executed[0]
    assert "pg_notify" in sql.lower()
    assert params[0] == "batch_progress"          # channel is a literal, not user data
    payload = json.loads(params[1])
    assert payload == {
        "batch_id": "batch-1",
        "event": "card_identified",
        "card_id": "card-9",
    }


def test_notify_progress_omits_absent_ids():
    pool = FakePool([[]])
    jobqueue.notify_progress(pool, "batch-1", "batch_complete")
    _sql, params = pool.cursor.executed[0]
    assert json.loads(params[1]) == {"batch_id": "batch-1", "event": "batch_complete"}


def test_claim_binds_allowed_types_into_sql():
    """The inner SELECT gains `AND type = ANY(%s)` bound to list(allowed_types)
    — parameterized, never interpolated. FakePool returns a detect row."""
    pool = FakePool([[("job-1", "detect", {"photo_id": "p1"})]])
    claimed = jobqueue.claim(pool, "w1", allowed_types=("detect", "identify"))
    assert claimed == ("job-1", "detect", {"photo_id": "p1"})
    sql, params = pool.cursor.executed[0]
    nospace = sql.lower().replace(" ", "")
    assert "type=any(%s)" in nospace                 # the type filter is present
    # params: (worker_id, [allowed types]) — the list is a bound parameter.
    assert params[0] == "w1"
    assert params[1] == ["detect", "identify"]       # list(), bound not interpolated


def test_claim_returns_none_when_no_matching_type():
    """No queued row of an allowed type -> FakePool yields [] -> None."""
    pool = FakePool([[]])
    assert jobqueue.claim(pool, "w1", allowed_types=("detect",)) is None


# ---- integration against the REAL local Postgres --------------------------

import uuid as _uuid  # noqa: E402  (kept beside the integration block)


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (export the compose-local DSN to run this)",
)
def test_claim_reclaim_backoff_against_real_db():
    """Insert synthetic jobs; verify FIFO claim order, SKIP LOCKED across two
    connections, requeue backoff on fail, and stale-row reclaim. Cleans up in
    finally so the jobs table is left as found."""
    import os as _os  # local, only for the skip-guarded path

    from psycopg_pool import ConnectionPool

    from notbulk import jobqueue as jq

    dsn = _os.environ["DATABASE_URL"]
    pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    tag = f"itest-{_uuid.uuid4().hex[:8]}"
    ids = [str(_uuid.uuid4()) for _ in range(3)]

    def _insert(job_id, seconds_ago):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (id, type, payload, status, created_at) "
                    "VALUES (%s, 'detect', %s, 'queued', now() - make_interval(secs => %s))",
                    (job_id, json.dumps({"photo_id": tag}), seconds_ago),
                )
            conn.commit()

    try:
        # Oldest created_at first -> claim FIFO.
        _insert(ids[0], 30)
        _insert(ids[1], 20)
        _insert(ids[2], 10)

        first = jq.claim(pool, "w1", allowed_types=("detect",))
        assert first is not None and first[0] == ids[0]
        assert first[1] == "detect"
        assert first[2] == {"photo_id": tag}

        # SKIP LOCKED: hold ids[1] FOR UPDATE on conn_a, claim on the pool must
        # skip it and return ids[2].
        conn_a = pool.getconn()
        try:
            with conn_a.cursor() as cur:
                cur.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (ids[1],))
                skipped = jq.claim(pool, "w2", allowed_types=("detect",))
                assert skipped is not None and skipped[0] == ids[2]
        finally:
            conn_a.rollback()
            pool.putconn(conn_a)

        # complete() the first job.
        jq.complete(pool, ids[0])
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id=%s", (ids[0],))
                assert cur.fetchone()[0] == "done"

        # fail(dead=False) requeues ids[2] with run_after in the future.
        jq.fail(pool, ids[2], "transient", dead=False)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, run_after > now() FROM jobs WHERE id=%s", (ids[2],)
                )
                status, in_future = cur.fetchone()
                assert status == "queued"
                assert in_future is True

        # reclaim: force ids[1] into a stale 'running' state, then reclaim it
        # back to 'queued' (attempts < max_attempts).
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET status='running', attempts=1, "
                    "locked_at=now() - interval '11 minutes' WHERE id=%s",
                    (ids[1],),
                )
            conn.commit()
        moved = jq.reclaim(pool)
        assert moved >= 1
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM jobs WHERE id=%s", (ids[1],))
                assert cur.fetchone()[0] == "queued"
    finally:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE payload->>'photo_id' = %s", (tag,))
            conn.commit()
        pool.close()


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (export the compose-local DSN to run this)",
)
def test_type_partitioned_claim_against_real_db():
    """A mix of queued types: a detect-only worker claims only detect jobs and
    SKIPs a queued price job; two allowed-type sets partition the queue cleanly
    (no job claimed twice, no job of the wrong class claimed). Self-cleaning."""
    import os as _os

    from psycopg_pool import ConnectionPool

    from notbulk import jobqueue as jq

    dsn = _os.environ["DATABASE_URL"]
    pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    tag = f"itest-{_uuid.uuid4().hex[:8]}"

    def _insert(job_type, payload):
        job_id = str(_uuid.uuid4())
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (id, type, payload, status) VALUES (%s, %s, %s, 'queued')",
                    (job_id, job_type, json.dumps(payload)),
                )
            conn.commit()
        return job_id

    try:
        d1 = _insert("detect", {"photo_id": tag})
        p1 = _insert("price", {"card_ref_id": tag, "finish": "normal"})
        d2 = _insert("detect", {"photo_id": tag})

        # A detect-only worker claims both detect jobs and never the price job.
        claimed_detect = []
        while True:
            c = jq.claim(pool, "w-detect", allowed_types=("detect",))
            if c is None:
                break
            if c[2].get("photo_id") == tag or c[2].get("card_ref_id") == tag:
                claimed_detect.append((c[0], c[1]))
        claimed_ids = {jid for jid, _ in claimed_detect}
        assert claimed_ids == {d1, d2}
        assert all(t == "detect" for _, t in claimed_detect)
        assert p1 not in claimed_ids                 # price job SKIPPED by type filter

        # The price job is still queued and claimable by a price-class worker.
        c_price = jq.claim(pool, "w-price", allowed_types=("price",))
        assert c_price is not None and c_price[0] == p1 and c_price[1] == "price"
    finally:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs WHERE payload->>'photo_id' = %s "
                    "OR payload->>'card_ref_id' = %s",
                    (tag, tag),
                )
            conn.commit()
        pool.close()
