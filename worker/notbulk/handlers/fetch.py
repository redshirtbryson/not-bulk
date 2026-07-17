"""fetch_source job handler (SSRF-gated).

Contract:
  photo row (status 'fetching') + source_url.
  - Album/gallery URL (imgur.com non-direct path, or a reddit post):
    enumerate -> fetch the FIRST image into THIS photo row; create additional
    photo rows + fetch_source jobs for the rest, truncated so the batch's total
    photo count stays <= quotas.photos_per_batch AND within the remaining daily
    usage.photos/usage.fetches quota (re-checked at enumeration time, not just
    the photos_per_batch cap -- the batch-create step only reserved 1
    photo + 1 fetch for the album URL itself; the fanout's remainder must be
    quota-accounted too, same conditional-upsert pattern as detect.py's
    cards-per-day reservation).
  - Direct URL: fetch_image -> gate_bytes -> storage.put(photo_key) -> photo
    'stored' + bytes + storage_bytes_used += -> enqueue detect ->
    notify_progress(photo_stored).
Rejections (FetchRejected/GateRejected) are PERMANENT: photo -> 'failed' +
notify, job COMPLETES (no retry). Transport errors propagate so the queue
retries via attempts.
"""
from __future__ import annotations

import sys
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

_PEEK_USAGE_SQL = (
    "SELECT COALESCE(photos, 0), COALESCE(fetches, 0) FROM usage "
    "WHERE user_id = %s AND day = current_date"
)

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

# Conditional photos+fetches reservation for the album fanout remainder,
# reserving an EXACT wanted amount (already clamped client-side to the
# observed headroom for both columns -- see _fanout). The WHERE guard on the
# UPDATE branch only lets the reservation through when both post-add totals
# stay within their caps, so a concurrent batch racing the same headroom can't
# over-grant: one of the two racers' UPDATE simply returns no row and that
# fanout falls back to re-reading (see _fanout's retry-once loop). Mirrors the
# INSERT-vs-UPDATE guard note from the web-layer checkAndReserve (M2 plan
# Task 7): the plain INSERT branch (first use today) is unguarded, which is
# safe here because `want` is pre-clamped to <= each cap by the caller.
_RESERVE_FANOUT_SQL = (
    "INSERT INTO usage (user_id, day, photos, fetches) "
    "VALUES (%(uid)s, current_date, %(want)s, %(want)s) "
    "ON CONFLICT (user_id, day) DO UPDATE SET "
    "photos = usage.photos + %(want)s, fetches = usage.fetches + %(want)s "
    "WHERE usage.photos + %(want)s <= %(photos_cap)s "
    "AND usage.fetches + %(want)s <= %(fetches_cap)s "
    "RETURNING user_id"
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


def _log_fanout_truncation(batch_id: str, offered: int, created: int,
                           bound: str) -> None:
    print(
        f"[fetch] batch {batch_id}: album offered {offered} additional "
        f"image(s), created {created} (truncated by {bound})",
        file=sys.stderr,
    )


def _reserve_fanout(pool, cfg, user_id, want) -> bool:
    """Atomically reserve `want` photos AND `want` fetches for today. Returns
    True if granted, False if the conditional UPDATE's guard rejected it (a
    concurrent reservation used up headroom between the caller's peek and this
    call). `want` must already be <= both caps (the plain-INSERT branch, first
    use of the day, is unguarded -- see _RESERVE_FANOUT_SQL comment)."""
    photos_cap = int(cfg["quotas"]["photos_per_day"])
    fetches_cap = int(cfg["quotas"]["fetches_per_day"])
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _RESERVE_FANOUT_SQL,
                {
                    "uid": user_id,
                    "want": want,
                    "photos_cap": photos_cap,
                    "fetches_cap": fetches_cap,
                },
            )
            granted = cur.fetchone() is not None
        conn.commit()
    return granted


def _fanout(pool, cfg, batch_id, user_id, extra_urls) -> None:
    """Create additional photo rows + fetch_source jobs for the album
    remainder, truncated to the SMALLEST of: room left under
    quotas.photos_per_batch for this batch, and the remaining daily headroom
    for BOTH usage.photos and usage.fetches for this user/day (re-checked at
    enumeration time -- the batch-create step only reserved 1 photo + 1 fetch
    for the album URL itself, not the fanout remainder).

    Usage is reserved atomically (conditional upsert, same guarded-UPDATE
    style as detect.py's cards-per-day reservation / the web layer's
    checkAndReserve) BEFORE any rows are created, so a concurrent batch can't
    over-grant. Since the reservation is all-or-nothing for a given `want`,
    a bounded retry loop re-peeks headroom and shrinks `want` if a race
    consumed quota between the peek and the reserve.
    """
    if not extra_urls:
        return
    batch_cap = int(cfg["quotas"]["photos_per_batch"])
    photos_cap = int(cfg["quotas"]["photos_per_day"])
    fetches_cap = int(cfg["quotas"]["fetches_per_day"])

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_BATCH_PHOTOS_SQL, (batch_id,))
            current = cur.fetchone()[0]
        conn.commit()
    batch_room = max(0, batch_cap - current)

    created = 0
    bound = "photos_per_batch"
    for _attempt in range(3):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_PEEK_USAGE_SQL, (user_id,))
                row = cur.fetchone()
            conn.commit()
        used_photos, used_fetches = row if row else (0, 0)
        photos_room = max(0, photos_cap - used_photos)
        fetches_room = max(0, fetches_cap - used_fetches)

        want = min(len(extra_urls), batch_room, photos_room, fetches_room)
        if want == min(len(extra_urls), batch_room):
            bound = "photos_per_batch"
        elif want == photos_room:
            bound = "daily photos quota"
        else:
            bound = "daily fetches quota"

        if want <= 0:
            created = 0
            break
        if _reserve_fanout(pool, cfg, user_id, want):
            created = want
            break
        # Race: headroom shrank between peek and reserve. Retry with a fresh
        # peek (bounded attempts so a persistently-racing quota can't loop
        # forever; worst case we truncate to 0 and let the next fanout retry).
    else:
        created = 0

    if created < len(extra_urls):
        _log_fanout_truncation(batch_id, len(extra_urls), created, bound)

    for url in extra_urls[:created]:
        new_photo_id = str(uuid7())
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_PHOTO_SQL,
                            (new_photo_id, batch_id, _source_type(url), url))
            conn.commit()
        jobqueue.enqueue(pool, "fetch_source", {"photo_id": new_photo_id},
                         batch_id=batch_id, user_id=user_id)
