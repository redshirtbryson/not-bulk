"""cv2-based image gate for FETCHED bytes (worker side), mirroring the Node
sharp gate for uploads. Enforces the SAME limits from the SAME config keys:
magic bytes (JPEG/PNG), byte-size cap, decode, pixel cap, re-encode to WebP q75.
"""
from __future__ import annotations

import cv2
import numpy as np

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class GateRejected(Exception):
    """Permanent rejection of fetched bytes."""


def gate_bytes(data: bytes, cfg: dict) -> tuple[bytes, int, int]:
    """Return (webp_bytes, width, height) or raise GateRejected."""
    max_bytes = int(cfg["quotas"]["max_photo_bytes"])
    max_pixels = int(cfg["quotas"]["max_pixels"])

    if len(data) > max_bytes:
        raise GateRejected("image too large (bytes)")
    if not (data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)):
        raise GateRejected("bad magic bytes (JPEG/PNG only)")

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise GateRejected("decode failed")
    h, w = img.shape[:2]
    if h * w > max_pixels:
        raise GateRejected("pixel cap exceeded")

    ok, buf = cv2.imencode(".webp", img, [cv2.IMWRITE_WEBP_QUALITY, 75])
    if not ok:
        raise GateRejected("webp re-encode failed")
    return buf.tobytes(), w, h
