"""In-memory augmentation for the reference hash index (design A4).

Each variant applies a random subset of degradations that mimic real capture
conditions, then a WebP q80 round-trip LAST so the augmented hash carries the
same codec fingerprint as a live query crop. Fully deterministic per (seed).
Augmentations are tuned to stay within match range — a variant that drifts out
of the accept distance is worse than useless.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import webp_roundtrip
from .hashing import REGIONS


def _homography_jitter(img, rng):
    """Perturb the 4 corners by <=2% of dims and warp."""
    h, w = img.shape[:2]
    dx, dy = 0.02 * w, 0.02 * h
    src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    jit = rng.uniform(-1, 1, (4, 2)).astype(np.float32) * np.array([dx, dy], np.float32)
    dst = (src + jit).astype(np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _white_balance(img, rng):
    """Per-channel gain in [0.92, 1.08]."""
    gains = rng.uniform(0.92, 1.08, 3).astype(np.float32)
    out = img.astype(np.float32) * gains[None, None, :]
    return np.clip(out, 0, 255).astype(np.uint8)


def _blur(img, rng):
    sigma = float(rng.uniform(0.0, 1.2))
    if sigma < 0.05:
        return img
    return cv2.GaussianBlur(img, (0, 0), sigmaX=sigma)


def _rotate(img, rng):
    """Rotation jitter <=3 deg about center."""
    h, w = img.shape[:2]
    angle = float(rng.uniform(-3.0, 3.0))
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _specular_sweep(img, rng):
    """Additive white linear-gradient band at a random angle across the art box
    (targets the holo/glare failure mode, design A4)."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = REGIONS["art"]
    ax0, ay0 = int(x0 * w), int(y0 * h)
    ax1, ay1 = int(x1 * w), int(y1 * h)
    alpha = float(rng.uniform(0.15, 0.35))
    angle = float(rng.uniform(0, np.pi))
    yy, xx = np.indices((ay1 - ay0, ax1 - ax0), dtype=np.float32)
    proj = xx * np.cos(angle) + yy * np.sin(angle)
    proj = (proj - proj.min()) / (np.ptp(proj) + 1e-6)  # np.ptp: ndarray.ptp was removed in NumPy 2.0
    band = np.exp(-((proj - rng.uniform(0.3, 0.7)) ** 2) / (2 * 0.12 ** 2))  # gaussian ridge
    add = (band * 255.0 * alpha).astype(np.float32)
    out = img.copy().astype(np.float32)
    roi = out[ay0:ay1, ax0:ax1]
    out[ay0:ay1, ax0:ax1] = np.clip(roi + add[..., None], 0, 255)
    return out.astype(np.uint8)


# Ordered pool of optional ops; webp_roundtrip is always applied last, outside this list.
_OPS = [_homography_jitter, _white_balance, _blur, _rotate, _specular_sweep]


def variants(img: np.ndarray, n: int, seed: int) -> list[np.ndarray]:
    """Return ``n`` deterministic augmented copies of ``img``.

    A fresh default_rng(seed) drives every choice, so the same (img, n, seed)
    always yields byte-identical output. Each variant applies a random subset
    of _OPS, then webp_roundtrip last.
    """
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    for _ in range(n):
        v = img.copy()
        # Random non-empty subset, applied in fixed op order for stability.
        while True:
            mask = rng.random(len(_OPS)) < 0.6
            if mask.any():
                break
        for op, use in zip(_OPS, mask):
            if use:
                v = op(v, rng)
        v = webp_roundtrip(v, quality=80)          # always last
        out.append(v)
    return out
