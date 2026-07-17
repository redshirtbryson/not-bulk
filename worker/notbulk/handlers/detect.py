"""detect job handler.

Contract (M2 plan, Worker handler behavior):
  load photo (must be status 'stored'; if already 'done' -> idempotent no-op)
  -> storage.get -> cv2.imdecode -> detect_cards
  -> reserve cards-per-day quota (first pass only), truncate to the granted
     allowance
  -> per Detection: uuid7 card id, INSERT cards ON CONFLICT (photo_id,
     crop_index) DO NOTHING RETURNING id; only when a row is RETURNED (a
     genuinely-new card) encode crop WebP, storage.put(crop_key), enqueue
     identify. A conflicting crop_index is skipped entirely.
  -> photo status 'done' -> notify_progress(photo_done).

Idempotent on retry at two levels:
  * whole-photo: a re-run after status 'done' returns before detect_cards.
  * per-crop: if the process dies mid-batch (some cards inserted+enqueued,
    photo still 'stored'), the retry re-runs detect_cards but the RETURNING
    branch skips every crop_index that already has a card row — so no orphaned
    crop blob is written (crop_key is keyed on the FRESH uuid7 card_id, which
    would never match the enqueued job) and no phantom identify job is enqueued
    for a card that was never inserted. Quota is charged once (first pass only).
"""
from __future__ import annotations

import sys

import cv2
import numpy as np
from uuid6 import uuid7

from .. import jobqueue
from ..detect import detect_cards

_SELECT_PHOTO_SQL = (
    "SELECT p.id, p.batch_id, p.status, b.user_id "
    "FROM photos p JOIN batches b ON b.id = p.batch_id WHERE p.id = %s"
)

# Conditional cards-per-day reservation. A CTE captures the row's PRIOR cards
# value, the UPSERT clamps the new total to the daily cap, and RETURNING reports
# the DELTA actually granted this call (new_total - prior) so the handler can
# truncate detections to exactly that many. First-of-day inserts see prior 0.
_RESERVE_CARDS_SQL = (
    "WITH prior AS ("
    "  SELECT COALESCE(cards, 0) AS cards FROM usage "
    "  WHERE user_id = %(uid)s AND day = current_date"
    ") "
    "INSERT INTO usage (user_id, day, cards) "
    "VALUES (%(uid)s, current_date, LEAST(%(want)s, %(cap)s)) "
    "ON CONFLICT (user_id, day) DO UPDATE SET "
    "cards = LEAST(usage.cards + %(want)s, %(cap)s) "
    "RETURNING usage.cards - COALESCE((SELECT cards FROM prior), 0)"
)

# RETURNING id yields exactly one row when this (photo_id, crop_index) is new
# and ZERO rows on conflict. The handler branches on that: a conflict means a
# prior (interrupted) run already created and enqueued this card, so it must be
# skipped to avoid an orphaned crop blob + phantom identify job.
_INSERT_CARD_SQL = (
    "INSERT INTO cards (id, photo_id, crop_index, status, rotation) "
    "VALUES (%s, %s, %s, 'pending', 0) "
    "ON CONFLICT (photo_id, crop_index) DO NOTHING "
    "RETURNING id"
)

# Existing card count for this photo. >0 means this is a RETRY of a photo whose
# first pass already reserved cards-per-day quota — the retry must not re-charge
# it, so reservation is skipped and detections flow straight into the INSERT
# where conflicts self-skip.
_COUNT_PHOTO_CARDS_SQL = "SELECT count(*) FROM cards WHERE photo_id = %s"

_SET_CROP_KEY_SQL = "UPDATE cards SET crop_storage_key = %s WHERE id = %s"

_MARK_PHOTO_DONE_SQL = "UPDATE photos SET status='done' WHERE id = %s"


def _log_truncation(photo_id: str, found: int, allowed: int) -> None:
    print(
        f"[detect] photo {photo_id}: {found} cards detected, quota allowed "
        f"{allowed}; truncated {found - allowed}",
        file=sys.stderr,
    )


def handle_detect(pool, storage, payload: dict, cfg: dict) -> None:
    photo_id = payload["photo_id"]
    cap = int(cfg["quotas"]["cards_per_day"])

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PHOTO_SQL, (photo_id,))
            row = cur.fetchone()
            if row is None:
                # Photo vanished (batch deleted). Nothing to do.
                return
            _id, batch_id, status, user_id = row
            if status == "done":
                # Idempotent: a prior run already finished this photo.
                return
        conn.commit()

    photo_bytes = storage.get(storage.photo_key(user_id, batch_id, photo_id))
    photo = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    detections = detect_cards(photo, cfg)

    # Detect a retry: cards already exist for this photo only if a prior
    # (interrupted) pass created them. That pass already reserved cards-per-day
    # quota, so a retry must NOT re-charge it — skip reservation and let the
    # per-detection INSERT's ON CONFLICT self-skip the already-processed crops.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COUNT_PHOTO_CARDS_SQL, (photo_id,))
            existing_cards = int(cur.fetchone()[0])
        conn.commit()

    if existing_cards == 0:
        # First pass: reserve today's cards-per-day quota; truncate detections
        # to the delta granted and log any truncation (no silent cap).
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _RESERVE_CARDS_SQL,
                    {"uid": user_id, "want": len(detections), "cap": cap},
                )
                granted = int(cur.fetchone()[0])
            conn.commit()
        granted = max(0, min(granted, len(detections)))
        if granted < len(detections):
            _log_truncation(photo_id, len(detections), granted)
        detections = detections[:granted]

    for det in detections:
        card_id = str(uuid7())
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_CARD_SQL, (card_id, photo_id, det.crop_index))
                inserted = cur.fetchone() is not None
            conn.commit()
        if not inserted:
            # Conflict: a prior interrupted pass already inserted+enqueued this
            # crop_index. Skip store/enqueue so we don't write an orphaned crop
            # blob (this card_id is fresh, unknown to the existing identify job)
            # or enqueue a phantom identify job for a card that wasn't inserted.
            continue
        ok, buf = cv2.imencode(
            ".webp", det.crop, [cv2.IMWRITE_WEBP_QUALITY, int(cfg["crop"]["webp_quality"])]
        )
        if not ok:
            raise RuntimeError("cv2.imencode('.webp') failed for crop")
        crop_key = storage.crop_key(user_id, batch_id, card_id)
        storage.put(crop_key, buf.tobytes(), "image/webp")
        # Persist the crop's storage key on the card row so identify (Task 12)
        # can fetch it without re-deriving it from photo/batch/user ids.
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SET_CROP_KEY_SQL, (crop_key, card_id))
            conn.commit()
        jobqueue.enqueue(pool, "identify", {"card_id": card_id},
                         batch_id=batch_id, user_id=user_id)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_MARK_PHOTO_DONE_SQL, (photo_id,))
        conn.commit()
    jobqueue.notify_progress(pool, batch_id, "photo_done", photo_id=photo_id)
