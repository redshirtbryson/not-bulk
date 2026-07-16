"""Card detection: adaptive-threshold contour path (design A7).

Downscale for detection, threshold + morphological close, find external
contours, keep card-aspect quads, then scale each quad back to the original
photo resolution and warp to the canonical crop.

Thresholding uses per-photo Otsu (global, but recomputed per image, hence
"adaptive" to each photo's own bimodal card/background split) rather than
cv2.adaptiveThreshold's local-neighborhood variant: a locally-adaptive
threshold with a positive constant C classifies any sufficiently flat region
(a plain table, mat, or binder background with near-zero local contrast) as
foreground, since pixel - local_mean ~= 0 > -C everywhere. That swallows the
card's own boundary as an interior hole of one giant frame-sized contour
instead of yielding a standalone card contour. Otsu's global split correctly
separates the card from a flat contrasting background.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import warp_card, sharpness
from .types import Detection

_DETECT_MAX_EDGE = 1600        # long-edge cap for the detection-only downscale
_APPROX_EPS_FRAC = 0.02        # approxPolyDP epsilon as fraction of perimeter


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
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    img_area = small.shape[0] * small.shape[1]
    min_area = min_area_frac * img_area

    quads: list[np.ndarray] = []                        # original-resolution quads
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
        quad = _order_corners(approx.astype(np.float32)) * inv   # back to original
        quads.append(quad)

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
