import numpy as np
import cv2
import pytest

from notbulk.preprocess import warp_card, webp_roundtrip, to_gray, sharpness

CFG = {"crop": {"width": 734, "height": 1024, "webp_quality": 80}}


def _canvas_with_rotated_rect():
    """Draw a filled, rotated rectangle on a black canvas and return
    (canvas, quad) where quad is the rect's 4 corners TL/TR/BR/BL."""
    canvas = np.zeros((900, 1200, 3), dtype=np.uint8)
    center = (600, 450)
    size = (400, 560)          # roughly card-aspect (0.714)
    angle = 18.0
    box = cv2.boxPoints(((center[0], center[1]), size, angle))  # (4,2) float32
    cv2.fillPoly(canvas, [box.astype(np.int32)], (255, 255, 255))
    # boxPoints order is bottom-left-ish going CW; reorder to TL/TR/BR/BL by geometry.
    s = box.sum(axis=1)
    d = np.diff(box, axis=1).ravel()
    tl = box[np.argmin(s)]
    br = box[np.argmax(s)]
    tr = box[np.argmin(d)]
    bl = box[np.argmax(d)]
    quad = np.array([tl, tr, br, bl], dtype=np.float32)
    return canvas, quad


def test_warp_card_produces_exact_crop_shape():
    canvas, quad = _canvas_with_rotated_rect()
    out = warp_card(canvas, quad, CFG)
    assert out.shape == (1024, 734, 3)
    assert out.dtype == np.uint8
    # The warped rectangle was solid white; the de-skewed crop is mostly white.
    assert out.mean() > 200


def test_webp_roundtrip_preserves_shape_and_dtype_but_degrades():
    # random-noise fixtures are pathological for lossy codecs; structured fixture reflects real crops.
    img = np.zeros((1024, 734, 3), dtype=np.uint8)
    img[:] = np.linspace(30, 220, 734, dtype=np.uint8)[np.newaxis, :, np.newaxis]
    cv2.rectangle(img, (60, 80), (400, 500), (200, 60, 40), thickness=-1)
    cv2.rectangle(img, (300, 600), (680, 950), (40, 180, 90), thickness=-1)
    cv2.rectangle(img, (450, 150), (700, 380), (90, 90, 230), thickness=-1)
    out = webp_roundtrip(img, quality=80)
    assert out.shape == img.shape
    assert out.dtype == img.dtype
    # Lossy codec: identical is essentially impossible, but stays close.
    assert not np.array_equal(out, img)
    assert np.abs(out.astype(np.int16) - img.astype(np.int16)).mean() < 10.0


def test_to_gray_returns_single_channel():
    img = np.full((10, 12, 3), 128, dtype=np.uint8)
    g = to_gray(img)
    assert g.shape == (10, 12)
    assert g.dtype == np.uint8


def test_sharpness_sharp_much_greater_than_blurred():
    # High-frequency checkerboard vs. its heavily blurred copy.
    tile = np.indices((256, 256)).sum(axis=0) % 2
    checker = (tile * 255).astype(np.uint8)
    checker = cv2.cvtColor(checker, cv2.COLOR_GRAY2BGR)
    blurred = cv2.GaussianBlur(checker, (0, 0), sigmaX=4.0)
    assert sharpness(checker) > sharpness(blurred) * 5


def test_sharpness_is_resolution_normalized():
    # Same content at two resolutions should give comparable (per-megapixel) scores.
    tile = np.indices((256, 256)).sum(axis=0) % 2
    small = cv2.cvtColor((tile * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    big = cv2.resize(small, (512, 512), interpolation=cv2.INTER_NEAREST)
    # Normalization keeps them within a factor of ~2 rather than 4x apart.
    ratio = sharpness(big) / sharpness(small)
    assert 0.4 < ratio < 2.5
