"""fetch_source job handler (SSRF-gated).

Contract:
  photo row (status 'fetching') + source_url.
  - Album/gallery URL (imgur.com non-direct path, or a reddit post):
    enumerate -> fetch the FIRST image into THIS photo row; create additional
    photo rows + fetch_source jobs for the rest, truncated so the batch's total
    photo count stays <= quotas.photos_per_batch and within the fetches quota.
  - Direct URL: fetch_image -> gate_bytes -> storage.put(photo_key) -> photo
    'stored' + bytes + storage_bytes_used += -> enqueue detect ->
    notify_progress(photo_stored).
Rejections (FetchRejected/GateRejected) are PERMANENT: photo -> 'failed' +
notify, job COMPLETES (no retry). Transport errors propagate so the queue
retries via attempts.
"""
from __future__ import annotations

from urllib.parse import urlparse

from uuid6 import uuid7

from .. import jobqueue
from ..fetcher import (FetchRejected, enumerate_imgur, enumerate_reddit,
                       fetch_image)
from ..imagegate import GateRejected, gate_bytes

_SELECT_PHOTO_SQL = (
    "SELECT p.id, p.batch_id, p.source_url, b.user_id "
    "FROM photos p JOIN batches b ON b.id = p.batch_id WHERE p.id = %s"
)

_COUNT_BATCH_PHOTOS_SQL = "SELECT count(*) FROM photos WHERE batch_id = %s"

_INSERT_PHOTO_SQL = (
    "INSERT INTO photos (id, batch_id, status, source_type, source_url) "
    "VALUES (%s, %s, 'fetching', %s, %s)"
)

_MARK_STORED_SQL = (
    "UPDATE photos SET status='stored', storage_key=%s, bytes=%s WHERE id=%s"
)

_MARK_FAILED_SQL = "UPDATE photos SET status='failed' WHERE id=%s"

_INC_STORAGE_SQL = (
    "UPDATE users SET storage_bytes_used = storage_bytes_used + %s WHERE id=%s"
)


def _is_album_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "imgur.com":
        return "/a/" in parsed.path or "/gallery/" in parsed.path
    if host == "www.reddit.com":
        return "/comments/" in parsed.path
    return False


def _source_type(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "reddit" if "redd" in host else "imgur"


def handle_fetch(pool, storage, payload: dict, cfg: dict) -> None:
    photo_id = payload["photo_id"]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PHOTO_SQL, (photo_id,))
            row = cur.fetchone()
            if row is None:
                return
            _id, batch_id, source_url, user_id = row
        conn.commit()

    try:
        if _is_album_url(source_url):
            urls = (enumerate_reddit(source_url, cfg)
                    if _source_type(source_url) == "reddit"
                    else enumerate_imgur(source_url, cfg))
            if not urls:
                raise FetchRejected("no images found in album/gallery")
            _store_direct(pool, storage, cfg, photo_id, batch_id, user_id, urls[0])
            _fanout(pool, cfg, batch_id, user_id, urls[1:])
        else:
            _store_direct(pool, storage, cfg, photo_id, batch_id, user_id, source_url)
    except (FetchRejected, GateRejected) as exc:
        # Permanent: mark the photo failed, notify, and let the job COMPLETE.
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_MARK_FAILED_SQL, (photo_id,))
            conn.commit()
        jobqueue.notify_progress(pool, batch_id, "photo_done", photo_id=photo_id)
        print(f"[fetch] photo {photo_id} rejected: {exc}")
        return
    # Transport / unexpected errors propagate -> job attempts retry.


def _store_direct(pool, storage, cfg, photo_id, batch_id, user_id, url) -> None:
    raw = fetch_image(url, cfg)
    webp, _w, _h = gate_bytes(raw, cfg)
    key = storage.photo_key(user_id, batch_id, photo_id)
    storage.put(key, webp, "image/webp")
    n = len(webp)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_MARK_STORED_SQL, (key, n, photo_id))
            cur.execute(_INC_STORAGE_SQL, (n, user_id))
        conn.commit()
    jobqueue.enqueue(pool, "detect", {"photo_id": photo_id},
                     batch_id=batch_id, user_id=user_id)
    jobqueue.notify_progress(pool, batch_id, "photo_stored", photo_id=photo_id)


def _fanout(pool, cfg, batch_id, user_id, extra_urls) -> None:
    """Create additional photo rows + fetch_source jobs for album remainder,
    truncated so the batch total stays within photos_per_batch."""
    cap = int(cfg["quotas"]["photos_per_batch"])
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_BATCH_PHOTOS_SQL, (batch_id,))
            current = cur.fetchone()[0]
        conn.commit()
    room = max(0, cap - current)
    for url in extra_urls[:room]:
        new_photo_id = str(uuid7())
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_PHOTO_SQL,
                            (new_photo_id, batch_id, _source_type(url), url))
            conn.commit()
        jobqueue.enqueue(pool, "fetch_source", {"photo_id": new_photo_id},
                         batch_id=batch_id, user_id=user_id)
