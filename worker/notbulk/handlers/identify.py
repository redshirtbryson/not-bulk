"""identify job handler.

Builds CascadeDeps ONCE per process (module-level lazy singleton), runs
identify_crop, then persists the full result:

- method_* columns from Identification.methods (by MethodResult.method letter),
- confidence / accepted_stage / rotation,
- candidates jsonb as [{"card_ref_id": id, "score": null}] — M1's Identification
  carries bare candidate ids with no per-candidate score, so score is null here
  (a contract nuance, flagged in the plan summary; M3 backfills real scores),
- status mapping: accepted_stage h/multi/llm -> 'auto', 'validation' ->
  'validation', 'unreadable' -> 'unreadable',
- finish gating (design A1): if card_ref_id resolved, read card_refs.finishes;
  len>1 -> finish_needs_confirmation=true AND downgrade 'auto' -> 'validation'
  (a deferred finish is NOT an auto-accept); len==1 -> set finish.

Then: increment usage.llm_calls when Method C ran, and fire the single-guarded
batch-completion UPDATE (fires at most once per batch).

Defensive: if the card_id no longer has a row (photo/batch/card deleted or a
phantom job), the initial SELECT returns nothing and the handler returns
cleanly without building CascadeDeps or touching identify_crop.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np

from .. import jobqueue
from ..cascade import CascadeDeps, identify_crop
from ..cli import resolve_config_path
from ..embed import Embedder
from ..hash_index import HashIndex
from ..ocr import OcrReader

_DEPS: CascadeDeps | None = None

_SELECT_CARD_SQL = (
    "SELECT c.id, b.id, b.user_id, c.crop_storage_key "
    "FROM cards c JOIN photos p ON p.id = c.photo_id "
    "JOIN batches b ON b.id = p.batch_id WHERE c.id = %s"
)

_SELECT_FINISHES_SQL = "SELECT finishes FROM card_refs WHERE id = %s"

_UPDATE_CARD_SQL = (
    "UPDATE cards SET card_ref_id=%s, finish=%s, finish_needs_confirmation=%s, "
    "confidence=%s, status=%s, accepted_stage=%s, rotation=%s, candidates=%s, "
    "method_h_id=%s, method_h_score=%s, method_a_id=%s, method_a_score=%s, "
    "method_b_id=%s, method_b_score=%s, method_c_id=%s, method_c_score=%s, "
    "updated_at=now() WHERE id=%s"
)

_INC_LLM_SQL = (
    "INSERT INTO usage (user_id, day, llm_calls) VALUES (%s, current_date, 1) "
    "ON CONFLICT (user_id, day) DO UPDATE SET llm_calls = usage.llm_calls + 1"
)

# Single-fire completion: only transitions a still-'processing' batch, and only
# when no photo is unfinished and no card is still pending. RETURNING id makes
# the fire observable exactly once (a racing worker sees 0 rows).
_COMPLETE_BATCH_SQL = (
    "UPDATE batches SET status='complete' WHERE id=%s AND status='processing' "
    "AND NOT EXISTS (SELECT 1 FROM photos WHERE batch_id=%s AND status <> 'done') "
    "AND NOT EXISTS (SELECT 1 FROM cards c JOIN photos p ON p.id=c.photo_id "
    "WHERE p.batch_id=%s AND c.status='pending') RETURNING id"
)


def _deps(cfg: dict, pool) -> CascadeDeps:
    """Build CascadeDeps once per process. ONNX/Qdrant/LLM are all optional and
    resolved against the config file's parent dir (M1 fix), matching cli.py."""
    global _DEPS
    if _DEPS is not None:
        return _DEPS

    hash_index = HashIndex.load(pool)
    cfg_path = resolve_config_path(None)
    onnx_path = str(Path(cfg_path).resolve().parent / cfg["models"]["embedding_onnx"])
    embedder = None
    qdrant = None
    if Path(onnx_path).is_file():
        embedder = Embedder(onnx_path)
        from qdrant_client import QdrantClient

        qdrant = QdrantClient(url=cfg["qdrant"]["url"])

    ocr_reader = OcrReader()

    anthropic_client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic

        anthropic_client = anthropic.Anthropic()

    _DEPS = CascadeDeps(
        hash_index=hash_index,
        embedder=embedder,
        qdrant=qdrant,
        ocr_reader=ocr_reader,
        anthropic=anthropic_client,
        pool=pool,
    )
    return _DEPS


def _method_columns(methods) -> dict[str, tuple[str | None, float | None]]:
    """Map MethodResult list to {'h':(id,score), 'a':..., 'b':..., 'c':...}."""
    cols = {m: (None, None) for m in ("h", "a", "b", "c")}
    for r in methods:
        if r.method in cols:
            cols[r.method] = (r.card_ref_id, r.score)
    return cols


def _status_for_stage(stage: str) -> str:
    if stage in ("h", "multi", "llm"):
        return "auto"
    if stage == "validation":
        return "validation"
    return "unreadable"


def handle_identify(pool, storage, payload: dict, cfg: dict) -> None:
    card_id = payload["card_id"]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_CARD_SQL, (card_id,))
            row = cur.fetchone()
            if row is None:
                # Card/photo/batch vanished (or a phantom job). Nothing to do.
                return
            _cid, batch_id, user_id, crop_key = row
        conn.commit()

    deps = _deps(cfg, pool)
    crop_bytes = storage.get(crop_key)
    crop = cv2.imdecode(np.frombuffer(crop_bytes, np.uint8), cv2.IMREAD_COLOR)
    ident = identify_crop(crop, deps, cfg)

    status = _status_for_stage(ident.accepted_stage)
    finish = None
    finish_needs_confirmation = False

    if ident.card_ref_id is not None:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_FINISHES_SQL, (ident.card_ref_id,))
                frow = cur.fetchone()
            conn.commit()
        finishes = list(frow[0]) if frow and frow[0] else []
        if len(finishes) > 1:
            finish_needs_confirmation = True
            if status == "auto":
                status = "validation"   # design A1: deferred finish != auto-accept
        elif len(finishes) == 1:
            finish = finishes[0]

    cols = _method_columns(ident.methods)
    candidates_json = json.dumps(
        [{"card_ref_id": cid, "score": None} for cid in ident.candidates]
    )

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _UPDATE_CARD_SQL,
                (
                    ident.card_ref_id, finish, finish_needs_confirmation,
                    ident.confidence, status, ident.accepted_stage, ident.rotation,
                    candidates_json,
                    cols["h"][0], cols["h"][1],
                    cols["a"][0], cols["a"][1],
                    cols["b"][0], cols["b"][1],
                    cols["c"][0], cols["c"][1],
                    card_id,
                ),
            )
        conn.commit()

    jobqueue.notify_progress(pool, batch_id, "card_identified", card_id=card_id)

    # LLM usage: any Method C result means one API call was made this run.
    if any(m.method == "c" for m in ident.methods):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INC_LLM_SQL, (user_id,))
            conn.commit()

    # Single-guarded batch completion.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COMPLETE_BATCH_SQL, (batch_id, batch_id, batch_id))
            fired = cur.fetchone() is not None
        conn.commit()
    if fired:
        jobqueue.notify_progress(pool, batch_id, "batch_complete")
