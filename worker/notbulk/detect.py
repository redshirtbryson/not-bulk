"""Card detection: adaptive-threshold contour path (design A7).

Downscale for detection, binarize, morphological close, find external
contours, keep card-aspect quads, then scale each quad back to the original
photo resolution and warp to the canonical crop.

Binarization is the UNION of two complementary thresholds, each feeding the
same contour -> quad -> aspect filter, with the accepted quads deduped by
centroid proximity:

- Per-photo Otsu (``THRESH_BINARY + THRESH_OTSU``): splits the photo's global
  card/background intensity bimodality; robust when lighting is uniform.
- Locally adaptive (``ADAPTIVE_THRESH_GAUSSIAN_C`` + ``THRESH_BINARY_INV``):
  robust to illumination gradients across the photo, where a single global
  split merges bright background with the cards.

Polarity matters for the adaptive pass: with ``THRESH_BINARY`` and a positive
constant C, any flat region satisfies pixel > local_mean - C and floods to
foreground, swallowing card boundaries inside one frame-sized blob. With
``THRESH_BINARY_INV`` the same flat regions go to background and only real
local edges (card borders) survive as foreground.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import warp_card, sharpness
from .types import Detection

_DETECT_MAX_EDGE = 1600        # long-edge cap for the detection-only downscale
_ADAPT_BLOCK = 51              # adaptiveThreshold blockSize (odd)
_ADAPT_C = 5                   # adaptiveThreshold constant
_APPROX_EPS_FRAC = 0.02        # approxPolyDP epsilon as fraction of perimeter
_DEDUPE_CENTROID_FRAC = 0.25   # same-card centroid distance vs mean edge length


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points TL/TR/BR/BL. TL has min x+y, BR max x+y;
    TR has min (x-y), BL max (x-y)."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = (pts[:, 0] - pts[:, 1])
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmax(d)]
    bl = pts[np.argmin(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _candidate_quads(binary: np.ndarray, min_area: float,
                     aspect: float, tol: float) -> list[np.ndarray]:
    """Contour -> convex-quad -> card-aspect filter on one binarization.

    Returns ordered (TL/TR/BR/BL) quads in the binarized image's coords.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    quads: list[np.ndarray] = []
    for c in contours:
        if cv2.contourArea(c) < min_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, _APPROX_EPS_FRAC * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        # Rotation-safe aspect from minAreaRect: short-edge / long-edge.
        (_, _), (rw, rh), _ = cv2.minAreaRect(approx)
        if rw == 0 or rh == 0:
            continue
        short, long_ = (rw, rh) if rw <= rh else (rh, rw)
        rect_aspect = short / long_
        if abs(rect_aspect - aspect) > tol:
            continue
        quads.append(_order_corners(approx.astype(np.float32)))
    return quads


def _mean_edge_len(quad: np.ndarray) -> float:
    """Mean length of the quad's 4 sides."""
    return float(np.mean(np.linalg.norm(quad - np.roll(quad, -1, axis=0), axis=1)))


def _dedupe_quads(quads: list[np.ndarray]) -> list[np.ndarray]:
    """Collapse quads whose centroids are closer than ~25% of the pair's
    mean card edge length — the same card found by both binarizations."""
    kept: list[np.ndarray] = []
    for q in quads:
        cq = q.mean(axis=0)
        eq = _mean_edge_len(q)
        dup = False
        for k in kept:
            ck = k.mean(axis=0)
            limit = _DEDUPE_CENTROID_FRAC * 0.5 * (eq + _mean_edge_len(k))
            if float(np.linalg.norm(cq - ck)) < limit:
                dup = True
                break
        if not dup:
            kept.append(q)
    return kept


def detect_cards(photo: np.ndarray, cfg: dict) -> list[Detection]:
    """Detect all card-aspect quads in ``photo`` and warp each to a crop."""
    det = cfg["detection"]
    aspect = float(det["aspect"])
    tol = float(det["aspect_tolerance"])
    min_area_frac = float(det["min_area_frac"])
    max_cards = int(det["max_cards_per_photo"])

    h0, w0 = photo.shape[:2]
    long_edge = max(h0, w0)
    scale = min(1.0, _DETECT_MAX_EDGE / long_edge)      # downscale factor (<=1)
    if scale < 1.0:
        small = cv2.resize(photo, (int(w0 * scale), int(h0 * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = photo
    inv = 1.0 / scale                                   # small -> original

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    min_area = min_area_frac * small.shape[0] * small.shape[1]

    # Union of two binarizations (see module docstring).
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        _ADAPT_BLOCK, _ADAPT_C,
    )
    candidates = (_candidate_quads(otsu, min_area, aspect, tol)
                  + _candidate_quads(adaptive, min_area, aspect, tol))

    # Dedupe same-card quads found by both passes, then map back to
    # original resolution.
    quads = [q * inv for q in _dedupe_quads(candidates)]

    # Order by row band (top-to-bottom), then x (left-to-right).
    def _key(q):
        cy = float(q[:, 1].mean())
        cx = float(q[:, 0].mean())
        band = int(cy // (h0 * 0.15))                   # ~card-height row bands
        return (band, cx)

    quads.sort(key=_key)
    quads = quads[:max_cards]                            # cap per design S4/A-detection

    detections: list[Detection] = []
    for idx, quad in enumerate(quads):
        crop = warp_card(photo, quad, cfg)
        detections.append(
            Detection(
                quad=quad.astype(np.float32),
                crop=crop,
                sharpness=sharpness(crop),
                crop_index=idx,
            )
        )
    return detections
