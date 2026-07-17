"""ingest_correction job handler — the corrections flywheel.

card -> load crop bytes -> INSERT the corrections row (crop_hash = sha256 of the
raw stored crop webp bytes, predicted_ref_id from payload, actual_ref_id) ->
compute_hashes(decoded crop) -> 5 INSERTs to ref_hashes (uuid7 id,
card_ref_id = actual_ref_id from payload, source 'user_validated', signed-bigint
conversion REUSED from build_hash_index) -> enforce the per-(card_ref_id,
hash_type) cap by deleting the oldest user_validated rows beyond
cfg.hash.user_validated_cap_per_card. Node NEVER writes corrections (it lacks the
crop bytes for the NOT NULL crop_hash) — Assembly Resolution 5.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np
from uuid6 import uuid7

# REUSE the exact uint64 -> signed bigint conversion (no reimplementation).
# `scripts/` is not part of the packaged `notbulk` wheel, so add the worker
# root (two levels up from this file: worker/notbulk/handlers/ -> worker/) to
# sys.path once, then import. Guarded so repeated imports don't stack entries.
_WORKER_ROOT = str(Path(__file__).resolve().parents[2])
if _WORKER_ROOT not in sys.path:
    sys.path.insert(0, _WORKER_ROOT)
from scripts.build_hash_index import to_signed_bigint  # noqa: E402

from ..hashing import compute_hashes  # noqa: E402

_HASH_FIELDS = (
    ("full", "full"),
    ("edge", "edge"),
    ("region_art", "region_art"),
    ("region_name", "region_name"),
    ("region_text", "region_text"),
)

_SELECT_CARD_SQL = (
    "SELECT c.id, b.id, b.user_id, c.crop_storage_key "
    "FROM cards c JOIN photos p ON p.id = c.photo_id "
    "JOIN batches b ON b.id = p.batch_id WHERE c.id = %s"
)

_INSERT_CORRECTION_SQL = (
    "INSERT INTO corrections (id, card_id, crop_hash, predicted_ref_id, actual_ref_id) "
    "VALUES (%s, %s, %s, %s, %s)"
)

_INSERT_HASH_SQL = (
    "INSERT INTO ref_hashes (id, card_ref_id, hash_type, hash_bits, source) "
    "VALUES (%s, %s, %s, %s, %s)"
)

# Window-function eviction: keep the newest `cap` user_validated rows per
# (card_ref_id, hash_type); delete the rest. Ordered by id DESC (uuid7 ids are
# time-ordered, so newest ids sort last-in-first-kept).
_EVICT_SQL = (
    "DELETE FROM ref_hashes WHERE id IN ("
    "  SELECT id FROM ("
    "    SELECT id, row_number() OVER ("
    "      PARTITION BY card_ref_id, hash_type ORDER BY id DESC"
    "    ) AS rn FROM ref_hashes "
    "    WHERE source='user_validated' AND card_ref_id=%s"
    "  ) ranked WHERE rn > %s"
    ")"
)


def handle_ingest_correction(pool, storage, payload: dict, cfg: dict) -> None:
    card_id = payload["card_id"]
    actual_ref_id = payload["actual_ref_id"]
    predicted_ref_id = payload["predicted_ref_id"]   # str or None (Assembly Resolution 5)
    cap = int(cfg["hash"]["user_validated_cap_per_card"])

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_CARD_SQL, (card_id,))
            row = cur.fetchone()
            if row is None:
                return
            _cid, _batch_id, _user_id, crop_key = row
        conn.commit()

    crop_bytes = storage.get(crop_key)
    # crop_hash is the sha256 of the RAW stored webp bytes (before decode) — the
    # corrections table's crop_hash is NOT NULL and identifies the exact stored crop.
    crop_hash = hashlib.sha256(crop_bytes).hexdigest()
    crop = cv2.imdecode(np.frombuffer(crop_bytes, np.uint8), cv2.IMREAD_COLOR)
    if crop is None:
        return
    hashes = compute_hashes(crop)

    rows = [
        (str(uuid7()), actual_ref_id, name, to_signed_bigint(getattr(hashes, attr)),
         "user_validated")
        for name, attr in _HASH_FIELDS
    ]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Write the corrections row FIRST (Node never writes it — Assembly Resolution 5).
            cur.execute(
                _INSERT_CORRECTION_SQL,
                (str(uuid7()), card_id, crop_hash, predicted_ref_id, actual_ref_id),
            )
            cur.executemany(_INSERT_HASH_SQL, rows)
            cur.execute(_EVICT_SQL, (actual_ref_id, cap))
        conn.commit()
