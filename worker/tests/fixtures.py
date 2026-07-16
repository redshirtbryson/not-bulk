"""Synthetic photo fixtures for detection / hashing / cascade tests.

Places solid-bordered, card-aspect rectangles with distinct inner patterns on a
contrasting background at known positions, with optional rotation. No real card
images — everything here is deterministic and drawable with numpy + cv2.
"""
from __future__ import annotations

import cv2
import numpy as np

CARD_ASPECT = 0.714          # 2.5 / 3.5, matches detection.aspect


def card_spec(cx, cy, w, h, angle=0.0, inner="grad"):
    """One synthetic card: center (cx,cy), size (w,h), rotation ``angle`` deg,
    and an ``inner`` pattern key drawn inside the white border."""
    return {"cx": cx, "cy": cy, "w": w, "h": h, "angle": float(angle), "inner": inner}


def _inner_pattern(w, h, key):
    """Return a (h,w,3) BGR inner fill with a distinct pattern per key."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if key == "grad":
        col = np.linspace(20, 235, w, dtype=np.uint8)
        img[:] = np.repeat(col[None, :, None], h, axis=0)
    elif key == "checker":
        t = (np.indices((h, w)).sum(axis=0) // 16) % 2
        img[:] = (t[..., None] * 200 + 30).astype(np.uint8)
    elif key == "rings":
        yy, xx = np.indices((h, w))
        r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
        img[:] = (((r.astype(np.int32) // 10) % 2) * 200 + 30)[..., None].astype(np.uint8)
    elif key == "white":
        img[:] = 255
    elif key == "black":
        img[:] = 0
    else:
        img[:] = 128
    return img


def synthetic_photo(specs, bg=(30, 90, 30), size=(1600, 1200), bg_ramp=None):
    """Render cards onto a contrasting background.

    Args:
        specs: list of card_spec dicts.
        bg: BGR background color (contrasting, non-white).
        size: (height, width) of the output photo.
        bg_ramp: optional (lo, hi) grayscale brightness pair; when set, the
            background is a left-to-right linear illumination ramp from
            ``lo`` to ``hi`` instead of the flat ``bg`` color. Simulates
            uneven lighting across the photo.

    Returns:
        BGR uint8 photo of shape (size[0], size[1], 3).
    """
    h_img, w_img = size
    photo = np.zeros((h_img, w_img, 3), dtype=np.uint8)
    if bg_ramp is not None:
        lo, hi = bg_ramp
        ramp = np.linspace(lo, hi, w_img).astype(np.uint8)
        photo[:] = ramp[None, :, None]
    else:
        photo[:] = np.array(bg, dtype=np.uint8)
    for s in specs:
        w, h = int(s["w"]), int(s["h"])
        card = np.full((h, w, 3), 255, dtype=np.uint8)          # white border
        pad = max(6, int(min(w, h) * 0.06))
        inner = _inner_pattern(w - 2 * pad, h - 2 * pad, s["inner"])
        card[pad:h - pad, pad:w - pad] = inner
        # Rotate the card patch about its own center, then paste.
        m = cv2.getRotationMatrix2D((w / 2, h / 2), s["angle"], 1.0)
        cos, sin = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        m[0, 2] += (nw - w) / 2
        m[1, 2] += (nh - h) / 2
        rot = cv2.warpAffine(card, m, (nw, nh), borderValue=(0, 0, 0))
        mask = cv2.warpAffine(np.full((h, w), 255, np.uint8), m, (nw, nh))
        x0, y0 = int(s["cx"] - nw / 2), int(s["cy"] - nh / 2)
        for yy in range(nh):
            for xx in range(nw):
                if mask[yy, xx] and 0 <= y0 + yy < h_img and 0 <= x0 + xx < w_img:
                    photo[y0 + yy, x0 + xx] = rot[yy, xx]
    return photo
