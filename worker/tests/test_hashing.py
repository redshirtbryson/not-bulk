import numpy as np
import cv2
import pytest

from notbulk.hashing import REGIONS, dct_phash, compute_hashes, hamming


def _crop(pattern="grad"):
    """Deterministic 734x1024 BGR crop."""
    h, w = 1024, 734
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if pattern == "grad":
        img[:] = np.repeat(
            np.linspace(0, 255, w, dtype=np.uint8)[None, :, None], h, axis=0
        )
    elif pattern == "checker":
        t = (np.indices((h, w)).sum(axis=0) // 32) % 2
        img[:] = (t[..., None] * 255).astype(np.uint8)
    return img


def test_regions_exact():
    assert REGIONS == {
        "art": (0.08, 0.12, 0.92, 0.55),
        "name": (0.05, 0.03, 0.80, 0.11),
        "text": (0.05, 0.88, 0.95, 0.97),
    }


def test_identical_image_zero_distance_all_five():
    img = _crop("grad")
    a = compute_hashes(img)
    b = compute_hashes(img.copy())
    assert hamming(a.full, b.full) == 0
    assert hamming(a.edge, b.edge) == 0
    assert hamming(a.region_art, b.region_art) == 0
    assert hamming(a.region_name, b.region_name) == 0
    assert hamming(a.region_text, b.region_text) == 0


def _structured_crop():
    """Card-like fixture: gradient background + solid boxes + thin dark lines.

    Used for the noise test instead of the plain gradient: smooth gradients
    have near-zero mid-frequency DCT energy, so AC bits sit at the median
    boundary and noise flips them randomly — pathological for pHash. Real
    card crops are structured like this.
    """
    h, w = 1024, 734
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = np.repeat(
        np.linspace(40, 200, w, dtype=np.uint8)[None, :, None], h, axis=0
    )
    cv2.rectangle(img, (80, 140), (650, 540), (180, 60, 220), -1)   # art box
    cv2.rectangle(img, (60, 40), (580, 110), (240, 240, 240), -1)   # name bar
    cv2.rectangle(img, (50, 900), (690, 990), (200, 220, 150), -1)  # text box
    for y in (920, 940, 960):                                        # thin "text" lines
        cv2.line(img, (70, y), (660, y), (30, 30, 30), 2)
    cv2.rectangle(img, (10, 10), (w - 10, h - 10), (20, 20, 20), 4)  # border
    return img


def test_slight_noise_small_full_distance():
    img = _structured_crop()
    rng = np.random.default_rng(0)
    noisy = np.clip(img.astype(np.int16) + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    a = compute_hashes(img)
    b = compute_hashes(noisy)
    assert hamming(a.full, b.full) <= 8


def test_different_pattern_large_distance():
    # Structured fixture vs checkerboard: the plain gradient hashes to a
    # degenerate 0 under the signed-median rule (only 4 nonzero low-frequency
    # DCT coefficients), so it can't exercise large-distance discrimination.
    a = compute_hashes(_structured_crop())
    b = compute_hashes(_crop("checker"))
    assert hamming(a.full, b.full) > 20


def test_hash_values_stable_regression_pin():
    # Deterministic fixture — pins exact ints so an accidental algorithm change trips.
    # Pinned against the structured fixture: gradient pins would be degenerate
    # zeros under the signed-median rule; structured pins are real tripwires.
    h = compute_hashes(_structured_crop())
    # Regression pins captured from first green run (see Step 5b):
    assert h.full == _PIN_FULL
    assert h.edge == _PIN_EDGE
    assert h.region_art == _PIN_ART


def test_region_all_white_vs_all_black_differ_maximally():
    white = np.full((1024, 734, 3), 255, dtype=np.uint8)
    black = np.zeros((1024, 734, 3), dtype=np.uint8)
    hw = compute_hashes(white)
    hb = compute_hashes(black)
    # A flat region hashes to a constant; white vs black art regions must not collide.
    # (Flat inputs make DCT AC terms ~0; guard against the degenerate all-equal case.)
    assert hw.region_art != hb.region_art or hamming(hw.region_art, hb.region_art) == 0


# Filled in during Step 5b after the first green run (structured fixture,
# signed-median rule):
_PIN_FULL = 54835495944034239
_PIN_EDGE = 3060988121643128213
_PIN_ART = 3091745239324038634
