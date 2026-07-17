"""Postgres-backed job queue: claim / complete / fail / reclaim / enqueue and
the batch_progress NOTIFY helper.

Claim uses `FOR UPDATE SKIP LOCKED` so N workers never hand the same job out
twice. All SQL matches the Interface Contract in the M2 plan header verbatim.
Nothing user-supplied reaches SQL as text — the NOTIFY channel is a literal and
the JSON payload is a bound parameter to pg_notify (design: never string-format
a channel/payload).
"""
from __future__ import annotations

import json

from uuid6 import uuid7

# Per-type required payload keys. Extra keys are rejected so a malformed
# enqueue can never smuggle fields into a handler.
_REQUIRED_KEYS: dict[str, set[str]] = {
    "detect": {"photo_id"},
    "identify": {"card_id"},
    "fetch_source": {"photo_id"},
    "ingest_correction": {"card_id", "actual_ref_id", "predicted_ref_id"},
}

_BACKOFF_STEP_SECONDS = 30

# Exact claim SQL from the Interface Contract.
_CLAIM_SQL = (
    "UPDATE jobs SET status='running', locked_at=now(), locked_by=%s, "
    "attempts=attempts+1, updated_at=now() "
    "WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now() "
    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, type, payload"
)

_COMPLETE_SQL = (
    "UPDATE jobs SET status='done', updated_at=now() WHERE id=%s"
)

# Requeue with backoff keyed on the current attempts count (already incremented
# at claim). Self-terminating: once attempts have reached max_attempts a
# transient failure is promoted to 'failed' rather than requeued forever
# (Interface Contract: at max_attempts -> failed). run_after is only advanced on
# the requeue branch.
_FAIL_REQUEUE_SQL = (
    "UPDATE jobs SET "
    "status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, "
    "last_error=%s, locked_at=NULL, locked_by=NULL, "
    "run_after = CASE WHEN attempts >= max_attempts THEN run_after "
    "ELSE now() + make_interval(secs => %s * attempts) END, "
    "updated_at=now() WHERE id=%s RETURNING status"
)

_FAIL_DEAD_SQL = (
    "UPDATE jobs SET status='failed', last_error=%s, locked_at=NULL, "
    "locked_by=NULL, updated_at=now() WHERE id=%s RETURNING status"
)

# Reclaim: stale 'running' rows (locked > 10 min) go back to 'queued' when
# retries remain, else to 'failed'. Two guarded UPDATEs, one transaction.
_RECLAIM_REQUEUE_SQL = (
    "UPDATE jobs SET status='queued', locked_at=NULL, locked_by=NULL, "
    "updated_at=now() WHERE status='running' "
    "AND locked_at < now() - interval '10 minutes' AND attempts < max_attempts"
)

_RECLAIM_FAIL_SQL = (
    "UPDATE jobs SET status='failed', locked_at=NULL, locked_by=NULL, "
    "last_error='reclaim: exceeded max_attempts', updated_at=now() "
    "WHERE status='running' AND locked_at < now() - interval '10 minutes' "
    "AND attempts >= max_attempts"
)

_ENQUEUE_SQL = (
    "INSERT INTO jobs (id, type, payload, batch_id, user_id) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def validate_payload(job_type: str, payload: dict) -> dict:
    """Return payload unchanged if it has exactly the required string keys for
    job_type; raise ValueError otherwise."""
    required = _REQUIRED_KEYS.get(job_type)
    if required is None:
        raise ValueError(f"unknown job type {job_type!r}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    for key in required:
        if key not in payload:
            raise ValueError(f"payload missing key {key!r}")
        # predicted_ref_id (ingest_correction) is nullable — str or None per Assembly
        # Resolution 5. The key must be PRESENT (checked above) but may be None when
        # the card had no prior prediction. All other required keys stay str-required.
        if key == "predicted_ref_id":
            if payload[key] is not None and not isinstance(payload[key], str):
                raise ValueError("payload 'predicted_ref_id' must be a string or None")
            continue
        if not isinstance(payload[key], str):
            raise ValueError(f"payload {key!r} must be a string")
    for key in payload:
        if key not in required:
            raise ValueError(f"payload has unexpected key {key!r}")
    return payload


def backoff_seconds(attempts: int) -> int:
    """Deterministic backoff used by fail(dead=False): 30s * attempts."""
    return _BACKOFF_STEP_SECONDS * attempts


def claim(pool, worker_id: str) -> tuple[str, str, dict] | None:
    """Atomically claim the oldest runnable job. Returns (id, type, payload)
    or None. payload is already a dict (jsonb decodes to dict via psycopg)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (worker_id,))
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return None
    job_id, job_type, payload = row
    # psycopg returns jsonb as a Python object already; be defensive if a
    # driver hands back a str.
    if isinstance(payload, str):
        payload = json.loads(payload)
    return str(job_id), job_type, payload


def complete(pool, job_id: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COMPLETE_SQL, (job_id,))
        conn.commit()


def fail(pool, job_id: str, error: str, *, dead: bool) -> str:
    """Mark a job failed or requeued. Returns the terminal status ('failed' or
    'queued') so the caller can react (e.g. mark an identify card unreadable).

    dead=True -> status 'failed' (no retry). dead=False -> requeue with a
    run_after backoff of 30s * attempts (attempts already incremented at claim),
    self-terminating to 'failed' once attempts have reached max_attempts.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if dead:
                cur.execute(_FAIL_DEAD_SQL, (error, job_id))
            else:
                cur.execute(_FAIL_REQUEUE_SQL, (error, _BACKOFF_STEP_SECONDS, job_id))
            row = cur.fetchone()
        conn.commit()
    return row[0] if row else "failed"


def reclaim(pool) -> int:
    """Requeue or fail stale 'running' rows. Returns rows transitioned."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_RECLAIM_REQUEUE_SQL)
            requeued = cur.rowcount or 0
            cur.execute(_RECLAIM_FAIL_SQL)
            failed = cur.rowcount or 0
        conn.commit()
    return requeued + failed


def notify_progress(pool, batch_id: str, event: str, **ids: str) -> None:
    """NOTIFY batch_progress with a JSON payload {batch_id, event, **ids}.

    Uses pg_notify(channel, payload) with BOTH args bound as parameters — the
    channel name is a fixed literal and the payload is JSON built here, so no
    user data is ever interpolated into SQL text.
    """
    payload = {"batch_id": batch_id, "event": event}
    for key, value in ids.items():
        if value is not None:
            payload[key] = value
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", ("batch_progress", json.dumps(payload)))
        conn.commit()


def enqueue(pool, job_type: str, payload: dict, *, batch_id: str | None = None,
            user_id: str | None = None) -> str:
    """Insert a queued job (uuid7 id) and wake a worker via NOTIFY jobs_wake.

    payload is validated before insert so a bad chain-enqueue fails loudly at
    the producing handler rather than at the consuming one.
    """
    validate_payload(job_type, payload)
    job_id = str(uuid7())
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_ENQUEUE_SQL, (job_id, job_type, json.dumps(payload), batch_id, user_id))
            cur.execute("SELECT pg_notify(%s, %s)", ("jobs_wake", ""))
        conn.commit()
    return job_id
