"""Build the reference hash index into ref_hashes.

For every card_refs row with a local scan at data/refs/{id}.png:
  - letterbox to 734x1024 (references are flat scans — direct resize, NO warp),
    then WebP q80 round-trip so refs share the codec fingerprint of live crops,
  - emit 5 'reference' hash rows,
  - generate cfg.hash.augmentations_per_card variants and emit 5*n 'augmented' rows.

Idempotent: DELETE the ('reference','augmented') rows for the batch's card ids
before inserting, so a re-run refreshes without duplicating and never touches
'user_validated' rows (design A9 — additive, never wipes validated/augmented is
enforced by the source-scoped DELETE).

Real invocation:
    bws run -- uv run python scripts/build_hash_index.py --sets sv4
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np
from uuid6 import uuid7

# Allow `python scripts/build_hash_index.py` from the worker/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notbulk.cli import resolve_config_path
from notbulk.config import load_config
from notbulk.db import get_pool
from notbulk.preprocess import webp_roundtrip
from notbulk.hashing import compute_hashes
from notbulk.augment import variants

_HASH_TYPE_FIELDS = (
    ("full", "full"),
    ("edge", "edge"),
    ("region_art", "region_art"),
    ("region_name", "region_name"),
    ("region_text", "region_text"),
)
_REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
_INSERT_BATCH = 500


def letterbox(img: np.ndarray, cfg: dict) -> np.ndarray:
    """Resize a flat reference scan into the canonical 734x1024 frame,
    preserving aspect with black padding (no perspective warp)."""
    w = int(cfg["crop"]["width"])
    h = int(cfg["crop"]["height"])
    ih, iw = img.shape[:2]
    scale = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def to_signed_bigint(u: int) -> int:
    """uint64 -> signed 64-bit (two's complement) for Postgres bigint storage."""
    u &= 0xFFFFFFFFFFFFFFFF
    return u - (1 << 64) if u >= (1 << 63) else u


def stable_seed(card_ref_id: str) -> int:
    """Deterministic augmentation seed per card id (stable across runs)."""
    digest = hashlib.sha256(card_ref_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _hash_fields(h) -> list[tuple[str, int]]:
    return [(name, getattr(h, attr)) for name, attr in _HASH_TYPE_FIELDS]


def hash_rows_for_card(card_ref_id: str, img: np.ndarray, cfg: dict) -> list[tuple]:
    """Return insertable rows (id, card_ref_id, hash_type, hash_bits, source).

    5 'reference' rows from the letterboxed+webp reference, plus
    5 * augmentations_per_card 'augmented' rows. Never emits 'user_validated'.
    """
    n = int(cfg["hash"]["augmentations_per_card"])
    base = webp_roundtrip(letterbox(img, cfg), quality=int(cfg["crop"]["webp_quality"]))

    rows: list[tuple] = []
    for name, bits in _hash_fields(compute_hashes(base)):
        rows.append((str(uuid7()), card_ref_id, name, to_signed_bigint(bits), "reference"))

    for variant in variants(base, n=n, seed=stable_seed(card_ref_id)):
        for name, bits in _hash_fields(compute_hashes(variant)):
            rows.append((str(uuid7()), card_ref_id, name, to_signed_bigint(bits), "augmented"))
    return rows


def delete_sql() -> str:
    """Idempotency DELETE — scoped to generated sources, never user_validated,
    batched by a card_ref_id array parameter."""
    return (
        "DELETE FROM ref_hashes "
        "WHERE source IN ('reference','augmented') "
        "AND card_ref_id = ANY(%s)"
    )


def _select_card_ids(cur, sets: list[str] | None, limit: int | None) -> list[str]:
    clauses, params = [], []
    if sets:
        clauses.append("set_id = ANY(%s)")
        params.append(sets)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT id FROM card_refs {where} ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ref_hashes from local scans.")
    ap.add_argument("--sets", help="comma-separated set_ids, e.g. sv4,sv5")
    ap.add_argument("--limit", type=int, help="cap card count (smoke runs)")
    args = ap.parse_args()

    cfg = load_config(resolve_config_path(None))
    sets = args.sets.split(",") if args.sets else None
    pool = get_pool()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            card_ids = _select_card_ids(cur, sets, args.limit)

    print(f"[build_hash_index] {len(card_ids)} candidate cards")
    processed, skipped = 0, 0
    pending: list[tuple] = []
    pending_cards: list[str] = []

    def _flush():
        nonlocal pending, pending_cards
        if not pending_cards:
            return
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(delete_sql(), (pending_cards,))
                cur.executemany(
                    "INSERT INTO ref_hashes "
                    "(id, card_ref_id, hash_type, hash_bits, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    pending,
                )
            conn.commit()
        pending, pending_cards = [], []

    for card_ref_id in card_ids:
        path = _REFS_DIR / f"{card_ref_id}.png"
        if not path.exists():
            skipped += 1
            continue
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            skipped += 1
            continue
        pending.extend(hash_rows_for_card(card_ref_id, img, cfg))
        pending_cards.append(card_ref_id)
        processed += 1
        if len(pending) >= _INSERT_BATCH:
            _flush()
        if processed % 100 == 0:
            print(f"[build_hash_index] {processed} cards hashed")

    _flush()
    print(f"[build_hash_index] done: {processed} hashed, {skipped} skipped (no local scan)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
