import cv2
import numpy as np
import pytest

from notbulk import cascade
from notbulk.hash_index import HashIndex
from notbulk.hashing import compute_hashes, dct_phash
from notbulk.preprocess import to_gray
from notbulk.types import MethodResult


def _blank_crop(value=127):
    # 734 wide x 1024 tall BGR, mid-gray so sharpness/hash are deterministic
    return np.full((1024, 734, 3), value, dtype=np.uint8)


def _gradient_crop():
    """Deterministic, non-symmetric, STRUCTURED 734x1024 BGR card-like crop.

    Deviation from the brief: the brief's original fixture was a smooth
    diagonal gradient, which is pathological for DCT pHash (near-zero
    mid-frequency energy -> flat sharpness ~1.0, far below
    detection.sharpness_min=45.0) and would trip the cascade's sharpness
    gate before reaching the stages under test. Swapped for the project's
    standard structured fixture (gradient background + solid boxes + thin
    lines, as in tests/test_hashing.py::_structured_crop), asymmetric
    top/bottom so 90/180/270 rotations remain distinguishable. Pre-authorized
    per the known "smooth gradients/noise are pathological for DCT hashing"
    fixture lesson.
    """
    h, w = 1024, 734
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = np.repeat(
        np.linspace(40, 200, w, dtype=np.uint8)[None, :, None], h, axis=0
    )
    cv2.rectangle(img, (80, 140), (650, 540), (180, 60, 220), -1)   # art box
    cv2.rectangle(img, (60, 40), (580, 110), (240, 240, 240), -1)   # name bar (top)
    cv2.rectangle(img, (50, 900), (690, 990), (200, 220, 150), -1)  # text box (bottom)
    for y in (920, 940, 960):                                       # thin "text" lines
        cv2.line(img, (70, y), (660, y), (30, 30, 30), 2)
    cv2.rectangle(img, (10, 10), (w - 10, h - 10), (20, 20, 20), 4)  # border
    return img


def _index_from_crops(named_crops):
    # named_crops: list[(card_ref_id, crop_bgr)] -> HashIndex with full-hash rows only
    rows = []
    for cid, crop in named_crops:
        h = compute_hashes(crop)
        rows.append((cid, "full", h.full))
    return HashIndex.from_rows(rows)


def test_orient_no_match_returns_rotation_zero():
    crop = _blank_crop()
    # Empty index -> match_full_only returns None for every rotation.
    index = HashIndex.from_rows([])
    upright, rotation = cascade.orient(crop, index)
    assert rotation == 0
    assert upright.shape == (1024, 734, 3)


def test_orient_upside_down_card_picks_180():
    upright = _gradient_crop()
    index = _index_from_crops([("sv4-1", upright)])
    # Present the card rotated 180 degrees; orient must undo it.
    flipped = np.rot90(upright, k=2)
    corrected, rotation = cascade.orient(flipped, index)
    assert rotation == 180
    # Corrected crop hash should be within tolerance of the indexed upright hash.
    from notbulk.hashing import compute_hashes, hamming
    d = hamming(compute_hashes(corrected).full, compute_hashes(upright).full)
    assert d <= 2


def _cfg():
    return {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
    }


def _deps(index, *, embedder=None, qdrant=None, ocr_reader=None, anthropic=None, pool=None):
    return cascade.CascadeDeps(
        hash_index=index,
        embedder=embedder,
        qdrant=qdrant,
        ocr_reader=ocr_reader,
        anthropic=anthropic,
        pool=pool,
    )


def test_identify_crop_below_sharpness_is_unreadable(monkeypatch):
    crop = _blank_crop()  # flat image -> Laplacian variance ~0
    index = HashIndex.from_rows([])
    result = cascade.identify_crop(crop, _deps(index), _cfg())
    assert result.accepted_stage == "unreadable"
    assert result.card_ref_id is None
    assert result.confidence == 0
    assert result.rotation == 0


class _FakeHashIndex:
    """Stands in for HashIndex when we need to force a specific HashMatch.
    Provides the two methods cascade calls: match_full_only and match."""

    def __init__(self, full_hit, hash_match):
        self._full_hit = full_hit          # tuple(card_ref_id, distance) | None
        self._hash_match = hash_match      # HashMatch | None

    def match_full_only(self, full_hash):
        return self._full_hit

    def match(self, h, cfg):
        return self._hash_match


def test_identify_crop_hash_only_accept(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()  # sharp enough to clear the gate
    hm = HashMatch(card_ref_id="sv4-7", score=0.98, distance=2, margin=8, agreement=5)
    index = _FakeHashIndex(full_hit=("sv4-7", 2), hash_match=hm)
    # Embedder/ocr/anthropic all None -> only H runs; ensure they are never called.
    result = cascade.identify_crop(crop, _deps(index), _cfg())
    assert result.accepted_stage == "h"
    assert result.card_ref_id == "sv4-7"
    assert result.confidence == 85 + min(8, 10)  # == 93
    assert result.candidates == ["sv4-7"]
    assert [m.method for m in result.methods] == ["h"]


def test_identify_crop_two_agree_multi(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    # H matches sv4-9 but with LOW agreement (won't trigger hash-only accept).
    hm = HashMatch(card_ref_id="sv4-9", score=0.70, distance=9, margin=1, agreement=2)
    index = _FakeHashIndex(full_hit=("sv4-9", 9), hash_match=hm)

    # A (embed) agrees with H on sv4-9; B (ocr) disagrees.
    monkeypatch.setattr(
        cascade.embed_mod,
        "embed_match",
        lambda emb, q, crop_bgr: MethodResult("a", "sv4-9", 0.8),
    )
    monkeypatch.setattr(
        cascade.ocr_mod,
        "ocr_match",
        lambda reader, pool, crop_bgr: MethodResult("b", "sv4-99", 0.4),
    )
    # anthropic MUST NOT be called on the two-agree path; pass a raiser and None.
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=None, pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.accepted_stage == "multi"
    assert result.card_ref_id == "sv4-9"
    # mean(h=0.70, a=0.80) = 0.75 -> 90 + round(7.5) = 98
    assert result.confidence == 98
    assert set(m.method for m in result.methods) == {"h", "a", "b"}
    assert result.candidates[0] == "sv4-9"


def _llm_setup(monkeypatch, index, *, a_id, a_score, b_id, b_score, c_id, c_score):
    monkeypatch.setattr(
        cascade.embed_mod,
        "embed_match",
        lambda emb, q, crop_bgr: MethodResult("a", a_id, a_score),
    )
    monkeypatch.setattr(
        cascade.ocr_mod,
        "ocr_match",
        lambda reader, pool, crop_bgr: MethodResult("b", b_id, b_score),
    )
    monkeypatch.setattr(
        cascade.llm_mod,
        "llm_match",
        lambda client, pool, crop_bgr, cfg: MethodResult("c", c_id, c_score),
    )


def test_identify_crop_llm_agrees_below_threshold_is_validation(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    # H low agreement, all three of h/a/b disagree with each other.
    hm = HashMatch(card_ref_id="sv4-1", score=0.5, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # C agrees with H's sv4-1. partner=h(score 0.5), c score chosen so:
    # 70 + round(15 * mean(0.5, c)) = 79  -> mean = 0.6 -> c = 0.7
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.6, b_id="sv4-3",
               b_score=0.6, c_id="sv4-1", c_score=0.7)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.confidence == 79
    assert result.accepted_stage == "validation"       # 79 < auto_accept(80)
    assert result.card_ref_id == "sv4-1"


def test_identify_crop_llm_agrees_at_threshold_is_llm_accept(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    hm = HashMatch(card_ref_id="sv4-1", score=0.5, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # 70 + round(15 * mean(0.5, c)) = 81 -> round(15*mean)=11 -> mean=0.733 -> c≈0.966
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.6, b_id="sv4-3",
               b_score=0.6, c_id="sv4-1", c_score=0.9667)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.confidence == 81
    assert result.accepted_stage == "llm"             # 81 >= auto_accept(80)
    assert result.card_ref_id == "sv4-1"


def test_identify_crop_no_agreement_is_validation_with_candidates(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    hm = HashMatch(card_ref_id="sv4-1", score=0.55, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # Every method names a different card; C also disagrees -> no accept anywhere.
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.45, b_id="sv4-3",
               b_score=0.30, c_id="sv4-4", c_score=0.20)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.accepted_stage == "validation"
    assert result.card_ref_id is None
    assert result.confidence == min(60, int(round(100 * 0.55)))  # == 55
    # Top-3 distinct card_ref_ids by method score, highest first.
    assert result.candidates == ["sv4-1", "sv4-2", "sv4-3"]
