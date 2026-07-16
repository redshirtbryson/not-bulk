import dataclasses

import numpy as np
import pytest

from notbulk.types import (
    CropHashes,
    Detection,
    MethodResult,
    HashMatch,
    Identification,
)


def test_crophashes_is_frozen():
    h = CropHashes(full=1, edge=2, region_art=3, region_name=4, region_text=5)
    assert h.full == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.full = 99  # type: ignore[misc]


def test_identification_defaults():
    ident = Identification(
        card_ref_id="sv4-123",
        confidence=91,
        accepted_stage="h",
        rotation=0,
    )
    assert ident.methods == []
    assert ident.candidates == []
    # defaults must be independent instances, not a shared mutable
    other = Identification(
        card_ref_id=None, confidence=0, accepted_stage="unreadable", rotation=0
    )
    ident.methods.append(MethodResult(method="h", card_ref_id="sv4-123", score=1.0))
    assert other.methods == []


def test_detection_carries_crop_and_index():
    quad = np.zeros((4, 2), dtype=np.float32)
    crop = np.zeros((1024, 734, 3), dtype=np.uint8)
    d = Detection(quad=quad, crop=crop, sharpness=50.0, crop_index=2)
    assert d.crop.shape == (1024, 734, 3)
    assert d.crop_index == 2


def test_methodresult_and_hashmatch_fields():
    m = MethodResult(method="a", card_ref_id=None, score=0.4)
    assert m.method == "a" and m.card_ref_id is None
    hm = HashMatch(card_ref_id="sv4-1", score=0.9, distance=6, margin=5, agreement=4)
    assert hm.agreement == 4 and hm.distance == 6
