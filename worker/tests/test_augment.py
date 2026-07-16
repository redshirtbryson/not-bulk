import numpy as np
import cv2
import pytest

from notbulk.augment import variants
from notbulk.hashing import compute_hashes, hamming


def _ref_crop():
    """Structured card-like 734x1024 crop (augmentations must stay matchable).

    Not a radial gradient: gradient patterns have near-degenerate DCT hashes,
    so WebP round-trip alone consumes the whole 16-bit distance budget.
    """
    h, w = 1024, 734
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = np.repeat(                                   # horizontal gradient background
        np.linspace(60, 180, w, dtype=np.uint8)[None, :, None], h, axis=0
    )
    cv2.rectangle(img, (60, 40), (580, 110), (235, 235, 235), -1)   # name band
    cv2.rectangle(img, (80, 140), (650, 540), (180, 60, 220), -1)   # art box
    cv2.rectangle(img, (50, 880), (690, 990), (210, 225, 190), -1)  # text panel
    for y in (910, 935, 960):                                        # thin "text" lines
        cv2.line(img, (70, y), (660, y), (30, 30, 30), 2)
    return img


def test_length_equals_n():
    out = variants(_ref_crop(), n=6, seed=7)
    assert len(out) == 6


def test_deterministic_for_same_seed():
    a = variants(_ref_crop(), n=4, seed=42)
    b = variants(_ref_crop(), n=4, seed=42)
    for x, y in zip(a, b):
        assert np.array_equal(x, y)


def test_differs_for_different_seed():
    a = variants(_ref_crop(), n=4, seed=1)
    b = variants(_ref_crop(), n=4, seed=2)
    # At least one variant differs across seeds.
    assert any(not np.array_equal(x, y) for x, y in zip(a, b))


def test_variants_differ_from_original_but_stay_in_match_range():
    img = _ref_crop()
    ref_hash = compute_hashes(img).full
    out = variants(img, n=6, seed=11)
    for v in out:
        assert not np.array_equal(v, img)                 # actually augmented
        d = hamming(ref_hash, compute_hashes(v).full)
        assert d <= 16                                     # stays matchable (the whole point)


def test_shapes_preserved():
    img = _ref_crop()
    for v in variants(img, n=5, seed=3):
        assert v.shape == img.shape
        assert v.dtype == img.dtype
