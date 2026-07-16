import importlib
import sys

import numpy as np
import pytest


def _load_build_module():
    # scripts/ is not a package; load by path so no torch import happens.
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_embed_index.py"
    spec = importlib.util.spec_from_file_location("build_embed_index", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_torch_import_at_module_load():
    sys.modules.pop("torch", None)
    _load_build_module()
    assert "torch" not in sys.modules  # torch import is guarded inside export_onnx/main


def test_preprocess_ref_applies_webp_roundtrip_and_returns_tensor():
    mod = _load_build_module()
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)
    t = mod.preprocess_ref(crop, webp_quality=80)
    assert t.shape == (1, 3, 224, 224)
    assert t.dtype == np.float32


def test_build_point_uses_uuid5_and_payload():
    mod = _load_build_module()

    class _Emb:
        def embed(self, crop_bgr):
            return np.ones(384, dtype=np.float32) / np.sqrt(384)

    # Use the real qdrant models module for PointStruct construction.
    from qdrant_client import models as qmodels

    crop = np.zeros((1024, 734, 3), np.uint8)
    point = mod.build_point(_Emb(), qmodels, "sv4-123", crop)

    import uuid

    expected_id = str(uuid.uuid5(mod.CARD_REF_NAMESPACE, "sv4-123"))
    assert point.id == expected_id
    assert point.payload == {"card_ref_id": "sv4-123"}
    assert len(point.vector) == 384
    # Deterministic id: same card_ref_id -> same point id (idempotent upsert).
    assert mod.build_point(_Emb(), qmodels, "sv4-123", crop).id == expected_id
