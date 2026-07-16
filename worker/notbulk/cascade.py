from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import embed as embed_mod
from . import llm as llm_mod
from . import ocr as ocr_mod
from .hash_index import HashIndex
from .hashing import compute_hashes, dct_phash
from .preprocess import sharpness, to_gray
from .types import Identification, MethodResult

if TYPE_CHECKING:
    from notbulk.embed import Embedder
    from notbulk.ocr import OcrReader

# k90 rotations applied by np.rot90 for each target upright rotation label.
# Target label R means: the crop is currently rotated R degrees clockwise from
# upright, so we rotate it back by R degrees. np.rot90(k) rotates 90*k CCW.
_ROT_TO_K = {0: 0, 90: 1, 180: 2, 270: 3}


def orient(crop_bgr: np.ndarray, index: HashIndex) -> tuple[np.ndarray, int]:
    """Try all four 90-degree rotations, keep the one whose full-card pHash is
    closest to any indexed card. Ties or no match anywhere -> rotation 0.

    Returns (upright 734x1024 BGR crop, rotation in {0,90,180,270})."""
    target_h, target_w = crop_bgr.shape[0], crop_bgr.shape[1]

    best_rotation = 0
    best_distance: int | None = None
    best_crop = crop_bgr

    for rotation, k in _ROT_TO_K.items():
        rotated = np.rot90(crop_bgr, k=k) if k else crop_bgr
        # 90/270 swap width/height; re-resize back to the canonical crop size so
        # the DCT pHash is computed on a 734x1024 image in every rotation.
        if rotated.shape[0] != target_h or rotated.shape[1] != target_w:
            rotated = cv2.resize(
                rotated, (target_w, target_h), interpolation=cv2.INTER_AREA
            )
        gray = to_gray(rotated)
        full_hash = dct_phash(gray)
        hit = index.match_full_only(full_hash)
        if hit is None:
            continue
        _card_id, distance = hit
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_rotation = rotation
            best_crop = rotated

    if best_distance is None:
        return crop_bgr, 0
    return best_crop, best_rotation


@dataclass
class CascadeDeps:
    hash_index: HashIndex
    embedder: "Embedder | None"
    qdrant: object | None
    ocr_reader: "OcrReader | None"
    anthropic: object | None
    pool: object


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _top3_candidates(results: list[MethodResult]) -> list[str]:
    """Distinct card_ref_ids ordered by highest method score first, capped at 3."""
    best: dict[str, float] = {}
    for r in results:
        if r.card_ref_id is None:
            continue
        if r.card_ref_id not in best or r.score > best[r.card_ref_id]:
            best[r.card_ref_id] = r.score
    ordered = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [cid for cid, _score in ordered[:3]]


def identify_crop(crop_bgr: np.ndarray, deps: CascadeDeps, cfg: dict) -> Identification:
    """Run the cost-ordered cascade H -> A -> B -> C with spec 4.3 scoring.

    Zero wrong auto-accepts is a HARD invariant (design A1): any tie or missing
    agreement resolves toward the validation queue, never toward an accept."""
    det_cfg = cfg["detection"]
    casc_cfg = cfg["cascade"]

    # --- Sharpness gate (runs before anything else) ---
    if sharpness(crop_bgr) < det_cfg["sharpness_min"]:
        return Identification(
            card_ref_id=None,
            confidence=0,
            accepted_stage="unreadable",
            rotation=0,
            methods=[],
            candidates=[],
        )

    # --- Orientation ---
    upright, rotation = orient(crop_bgr, deps.hash_index)

    methods: list[MethodResult] = []

    # --- Stage 1: Method H ---
    hashes = compute_hashes(upright)
    hash_match = deps.hash_index.match(hashes, cfg)
    h_result = MethodResult(
        method="h",
        card_ref_id=hash_match.card_ref_id if hash_match else None,
        score=hash_match.score if hash_match else 0.0,
    )
    methods.append(h_result)

    # High-margin ensemble accept: all three hash CATEGORIES agree (full + edge +
    # majority of the three region hashes). HashIndex.match encodes this as
    # agreement across the 5 hash types; require agreement on at least the full,
    # edge, and 2/3 region hashes -> agreement >= 4 of 5.
    if hash_match is not None and hash_match.agreement >= 4:
        score = 85 + min(hash_match.margin, 10)
        if score >= casc_cfg["hash_only_accept"]:
            return Identification(
                card_ref_id=hash_match.card_ref_id,
                confidence=score,
                accepted_stage="h",
                rotation=rotation,
                methods=methods,
                candidates=_top3_candidates(methods),
            )

    # --- Stage 2: Methods A + B (collected alongside H) ---
    if deps.embedder is not None and deps.qdrant is not None:
        methods.append(embed_mod.embed_match(deps.embedder, deps.qdrant, upright))
    if deps.ocr_reader is not None:
        methods.append(ocr_mod.ocr_match(deps.ocr_reader, deps.pool, upright))

    # Any two of {h,a,b} agree on the same card_ref_id -> base 90 + up to 10.
    hab = [m for m in methods if m.method in ("h", "a", "b") and m.card_ref_id is not None]
    agree_id, agree_pair = _find_agreeing_pair(hab)
    if agree_id is not None:
        mean_score = sum(m.score for m in agree_pair) / len(agree_pair)
        confidence = 90 + int(round(10 * mean_score))
        confidence = _clamp(confidence, 0, 100)
        # Stage 2 auto-accepts by definition (>= auto_accept always holds here).
        return Identification(
            card_ref_id=agree_id,
            confidence=confidence,
            accepted_stage="multi",
            rotation=rotation,
            methods=methods,
            candidates=_top3_candidates(methods),
        )

    # --- Stage 3: Method C (LLM tiebreaker), only if enabled ---
    if deps.anthropic is not None:
        c_result = llm_mod.llm_match(deps.anthropic, deps.pool, upright, cfg)
        methods.append(c_result)
        if c_result.card_ref_id is not None:
            # Does C agree with any of h/a/b?
            partner = next(
                (m for m in hab if m.card_ref_id == c_result.card_ref_id), None
            )
            if partner is not None:
                mean_score = (c_result.score + partner.score) / 2
                confidence = _clamp(70 + int(round(15 * mean_score)), 0, 85)
                if confidence >= casc_cfg["auto_accept"]:
                    return Identification(
                        card_ref_id=c_result.card_ref_id,
                        confidence=confidence,
                        accepted_stage="llm",
                        rotation=rotation,
                        methods=methods,
                        candidates=_top3_candidates(methods),
                    )
                return Identification(
                    card_ref_id=c_result.card_ref_id,
                    confidence=confidence,
                    accepted_stage="validation",
                    rotation=rotation,
                    methods=methods,
                    candidates=_top3_candidates(methods),
                )

    # --- No agreement anywhere: validation with top-3 candidates ---
    best = max((m.score for m in methods), default=0.0)
    confidence = min(60, int(round(100 * best)))
    return Identification(
        card_ref_id=None,
        confidence=confidence,
        accepted_stage="validation",
        rotation=rotation,
        methods=methods,
        candidates=_top3_candidates(methods),
    )


def _find_agreeing_pair(
    results: list[MethodResult],
) -> tuple[str | None, list[MethodResult]]:
    """Return (card_ref_id, [the two+ results that agree on it]) for the highest-
    scoring agreeing group, or (None, []). Agreement = same card_ref_id from >=2
    distinct methods."""
    by_id: dict[str, list[MethodResult]] = {}
    for r in results:
        by_id.setdefault(r.card_ref_id, []).append(r)
    agreeing = {cid: rs for cid, rs in by_id.items() if len(rs) >= 2}
    if not agreeing:
        return None, []
    best_id = max(
        agreeing,
        key=lambda cid: sum(r.score for r in agreeing[cid]) / len(agreeing[cid]),
    )
    return best_id, agreeing[best_id]
