"""gate_bytes: magic-byte, pixel-cap, and size-cap rejects + a happy path that
re-encodes to WebP."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from notbulk.imagegate import gate_bytes, GateRejected

CFG = {"quotas": {"max_photo_bytes": 10485760, "max_pixels": 50000000}}


def _jpeg_bytes(h=64, w=48):
    img = np.zeros((h, w, 3), np.uint8)
    cv2.rectangle(img, (5, 5), (w - 5, h - 5), (200, 60, 60), -1)  # structured
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_gate_accepts_jpeg_and_returns_webp():
    webp, width, height = gate_bytes(_jpeg_bytes(), CFG)
    assert webp[:4] == b"RIFF"          # WebP container magic
    assert (width, height) == (48, 64)


def test_gate_rejects_bad_magic():
    with pytest.raises(GateRejected, match="magic"):
        gate_bytes(b"not-an-image-at-all", CFG)


def test_gate_rejects_oversize_bytes():
    cfg = {"quotas": {"max_photo_bytes": 10, "max_pixels": 50000000}}
    with pytest.raises(GateRejected, match="too large"):
        gate_bytes(_jpeg_bytes(), cfg)


def test_gate_rejects_pixel_cap():
    cfg = {"quotas": {"max_photo_bytes": 10485760, "max_pixels": 100}}
    with pytest.raises(GateRejected, match="pixel"):
        gate_bytes(_jpeg_bytes(64, 48), cfg)     # 3072 px > 100
