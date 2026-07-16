"""Crop-normalization primitives shared by detection, hashing, and augmentation.

Every user crop and every reference image passes through the identical pipeline:
perspective warp to the canonical 734x1024 frame, then a WebP q80 round-trip so
the index and the query share the codec fingerprint (design A4).
"""
from __future__ import annotations

import cv2
import numpy as np


def warp_card(photo: np.ndarray, quad: np.ndarray, cfg: dict) -> np.ndarray:
    """De-skew the card bounded by ``quad`` into the canonical crop.

    Args:
        photo: BGR uint8 source photo.
        quad: (4,2) float32 corner coords in source-photo space,
            ordered TL/TR/BR/BL.
        cfg: config dict; uses cfg["crop"]["width"|"height"].

    Returns:
        BGR uint8 array of shape (height, width, 3) == (1024, 734, 3).
    """
    w = int(cfg["crop"]["width"])
    h = int(cfg["crop"]["height"])
    src = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    dst = np.array(
        [[0.0, 0.0], [w - 1.0, 0.0], [w - 1.0, h - 1.0], [0.0, h - 1.0]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(photo, m, (w, h), flags=cv2.INTER_LINEAR)


def webp_roundtrip(img: np.ndarray, quality: int = 80) -> np.ndarray:
    """Encode to WebP at ``quality`` and decode back, imprinting the codec
    fingerprint that index and query crops must share."""
    ok, buf = cv2.imencode(".webp", img, [cv2.IMWRITE_WEBP_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode('.webp') failed")
    out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if out is None:
        raise RuntimeError("cv2.imdecode('.webp') failed")
    return out


def to_gray(img: np.ndarray) -> np.ndarray:
    """BGR -> single-channel uint8 grayscale."""
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def sharpness(img: np.ndarray) -> float:
    """Laplacian-variance sharpness, normalized per megapixel.

    Normalizing by megapixels (var / (h*w/1e6)) makes the threshold in
    config.yaml (detection.sharpness_min) resolution-independent (design A8):
    the score must not grow with pixel count for the same content, so a
    high-resolution blurry photo can never outscore a low-resolution sharp one.
    """
    gray = to_gray(img) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    megapixels = (h * w) / 1e6
    if megapixels <= 0:
        return 0.0
    return var / megapixels
