"""Worker main loop.

- Loads config + pool + Storage, registers handlers by job type.
- LISTENs `jobs_wake` on a dedicated connection (wake-only; 5 s fallback poll).
- Drains all claimable jobs, then blocks on notifies with a 5 s timeout.
- Reclaims stale jobs every 60 s.
- SIGTERM/SIGINT: finishes the in-flight job, then exits 0.
- Per job: validate_payload -> dispatch -> complete, or fail with a SANITIZED
  last_error (str(exc), not the full traceback — the Discord-sink lesson: never
  persist a raw traceback into a user-visible/error column). The full traceback
  goes to stderr only.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import time
import traceback

from psycopg_pool import ConnectionPool

from . import discord
from . import jobqueue
from .cli import resolve_config_path
from .config import load_config
from .handlers import detect as detect_handler
from .handlers import fetch as fetch_handler
from .handlers import identify as identify_handler
from .handlers import price as price_handler
from .handlers.correction import handle_ingest_correction
from .storage import Storage

_POLL_TIMEOUT_SECONDS = 5.0
_RECLAIM_EVERY_SECONDS = 60.0


def _build_handlers():
    return {
        "detect": detect_handler.handle_detect,
        "identify": identify_handler.handle_identify,
        "fetch_source": fetch_handler.handle_fetch,
        "ingest_correction": handle_ingest_correction,
        "price": price_handler.handle_price,
    }


class _Stopper:
    """Cooperative shutdown flag set by SIGTERM/SIGINT."""

    def __init__(self):
        self.stop = False

    def request(self, *_a):
        self.stop = True


def _process_one(pool, storage, cfg, handlers, worker_id: str) -> bool:
    """Claim and run a single job. Returns True if a job was processed."""
    claimed = jobqueue.claim(pool, worker_id)
    if claimed is None:
        return False
    job_id, job_type, payload = claimed
    handler = handlers.get(job_type)
    try:
        if handler is None:
            raise ValueError(f"no handler for job type {job_type!r}")
        jobqueue.validate_payload(job_type, payload)
        handler(pool, storage, payload, cfg)
        jobqueue.complete(pool, job_id)
    except Exception as exc:  # noqa: BLE001 — worker must never crash on a bad job
        # Full trace to stderr; only str(exc) is persisted (sanitized).
        traceback.print_exc()
        dead = _is_permanent(exc)
        # fail() returns the TERMINAL status: 'failed' when this exhausted the
        # last attempt (or dead=True), else 'queued'. Only a truly-failed
        # identify job marks its card unreadable (Interface Contract).
        terminal = jobqueue.fail(pool, job_id, str(exc), dead=dead)
        if terminal == "failed":
            # Sanitized error notify (design S10): error CLASS + ids only — never
            # str(exc) / a traceback (which can carry interpolated user content).
            discord.notify(
                cfg, "error", "pipeline job failed",
                {
                    "type": job_type,
                    "job_id": job_id,
                    "batch_id": str(payload.get("batch_id") or "n/a"),
                    "error_class": exc.__class__.__name__,
                },
            )
            if job_type == "identify":
                _mark_card_unreadable(pool, payload.get("card_id"), str(exc))
    return True


def _is_permanent(exc: Exception) -> bool:
    """Whether an exception should fail the job immediately (no retry).

    ValueError covers bad-payload / validation / no-handler errors — retrying
    can't help. FetchRejected and GateRejected (permanent fetch/gate rejects)
    are handled INSIDE the fetch handler, which completes the job itself, so
    they never surface here. Everything else is transient: the queue's
    max_attempts caps retries and fail(dead=False) self-terminates to 'failed'
    at the last attempt.
    """
    return isinstance(exc, ValueError)


def _mark_card_unreadable(pool, card_id, error: str) -> None:
    if not card_id:
        return
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cards SET status='unreadable', updated_at=now() WHERE id=%s",
                (card_id,),
            )
        conn.commit()


def main() -> None:
    cfg_path = resolve_config_path(None)
    cfg = load_config(cfg_path)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set; run under `bws run`")
    pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    storage = Storage(cfg)
    handlers = _build_handlers()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    stopper = _Stopper()
    signal.signal(signal.SIGTERM, stopper.request)
    signal.signal(signal.SIGINT, stopper.request)

    # Startup reclaim, then a dedicated LISTEN connection.
    # NOTE: this LISTEN connection is checked out of the pool for the worker's entire
    # lifetime, permanently occupying 1 of the max_size=4 pool slots (3 remain for job
    # processing). Acceptable for the single-worker M2 topology. If worker-pool pressure
    # appears (M4+ concurrent workers), refactor to a dedicated standalone connection
    # opened outside the pool rather than borrowing a pool slot. Logged follow-up.
    jobqueue.reclaim(pool)
    listen_conn = pool.getconn()
    listen_conn.autocommit = True
    listen_conn.execute("LISTEN jobs_wake")

    last_reclaim = time.monotonic()
    print(f"[worker] up as {worker_id}; waiting for jobs", file=sys.stderr)
    try:
        while not stopper.stop:
            # Drain everything claimable right now.
            while not stopper.stop and _process_one(pool, storage, cfg, handlers, worker_id):
                pass

            if time.monotonic() - last_reclaim >= _RECLAIM_EVERY_SECONDS:
                jobqueue.reclaim(pool)
                last_reclaim = time.monotonic()

            if stopper.stop:
                break

            # Block for a notify, waking at least every 5 s to re-poll/reclaim.
            # psycopg3 idiom (>=3.2; pyproject pins >=3.3.4): the notifies()
            # generator yields each queued notification then returns when the
            # timeout elapses OR stop_after notifications have been seen. We only
            # need the wake signal, so stop_after=1 returns as soon as one
            # arrives; draining ALL claimable jobs happens on the next loop pass.
            # A dedicated autocommit connection is required for timely delivery.
            for _note in listen_conn.notifies(timeout=_POLL_TIMEOUT_SECONDS, stop_after=1):
                pass  # consume the wake; loop back to drain
    finally:
        try:
            listen_conn.execute("UNLISTEN jobs_wake")
        finally:
            pool.putconn(listen_conn)
        pool.close()
    print("[worker] stopped cleanly", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
