"""64-bit DCT perceptual hashing for the Stage-1 ensemble.

Five hashes per crop: full-crop pHash, an edge-map pHash (Sobel magnitude),
and three region pHashes (art / name / text). Region fractions are of the
canonical 734x1024 frame.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import to_gray
from .types import CropHashes

REGIONS: dict[str, tuple[float, float, float, float]] = {
    "art": (0.08, 0.12, 0.92, 0.55),
    "name": (0.05, 0.03, 0.80, 0.11),
    "text": (0.05, 0.88, 0.95, 0.97),
}


def dct_phash(gray: np.ndarray) -> int:
    """64-bit DCT pHash of a single-channel grayscale image.

    Input must already be single-channel (use ``to_gray`` first). Resize to
    32x32 float32, take the 2D DCT, keep the top-left 8x8 block (low
    frequencies), drop the [0,0] DC term, and set each of the remaining
    63 bits where the coefficient exceeds the signed median of the 63 AC
    coefficients. Bit 0 (the DC slot) is always 0, giving a stable 64-bit
    packing. The signed rule preserves coefficient sign (structure), which
    measurably improves intra/inter discrimination over an absolute-value
    rule on real card crops.
    """
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(resized)
    block = dct[:8, :8]                      # 8x8 low-frequency coefficients
    flat = block.flatten()                   # 64 coefficients, [0] is DC
    med = np.median(flat[1:])                # signed median over the 63 AC coefficients
    bits = 0
    for i in range(64):
        bit = 1 if (i != 0 and flat[i] > med) else 0
        bits = (bits << 1) | bit
    return int(bits)


def _region_gray(gray: np.ndarray, frac: tuple[float, float, float, float]) -> np.ndarray:
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = frac
    xa, ya = int(round(x0 * w)), int(round(y0 * h))
    xb, yb = int(round(x1 * w)), int(round(y1 * h))
    return gray[ya:yb, xa:xb]


def _edge_map(gray: np.ndarray) -> np.ndarray:
    """Sobel-magnitude edge map, normalized to uint8."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mx = float(mag.max())
    if mx <= 0:
        return np.zeros_like(gray, dtype=np.uint8)
    return np.clip(mag / mx * 255.0, 0, 255).astype(np.uint8)


def compute_hashes(crop_bgr: np.ndarray) -> CropHashes:
    """Compute the 5-hash ensemble for a canonical 734x1024 BGR crop."""
    gray = to_gray(crop_bgr)
    full = dct_phash(gray)
    edge = dct_phash(_edge_map(gray))
    region_art = dct_phash(_region_gray(gray, REGIONS["art"]))
    region_name = dct_phash(_region_gray(gray, REGIONS["name"]))
    region_text = dct_phash(_region_gray(gray, REGIONS["text"]))
    return CropHashes(
        full=full,
        edge=edge,
        region_art=region_art,
        region_name=region_name,
        region_text=region_text,
    )


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit hashes via int.bit_count()."""
    return int((a ^ b).bit_count())
