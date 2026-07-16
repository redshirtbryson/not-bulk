"""Method A: DINOv2 ViT-S/14 embedding matcher (CPU ONNX at runtime).

Method A is a shortlist generator (design A2): it produces a candidate + score
but never auto-accepts alone. Gating happens in the cascade (Task 14).
"""
from __future__ import annotations

import cv2
import numpy as np
import onnxruntime as ort

from notbulk.types import MethodResult

QDRANT_COLLECTION = "card_refs"

# ImageNet statistics (RGB order), matching DINOv2 preprocessing.
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_INPUT_SIZE = 224  # multiple of the ViT-S/14 patch size (14)


def preprocess_to_tensor(crop_bgr: np.ndarray) -> np.ndarray:
    """BGR uint8 crop -> (1,3,224,224) float32 NCHW, ImageNet-normalized RGB."""
    rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (_INPUT_SIZE, _INPUT_SIZE), interpolation=cv2.INTER_AREA)
    arr = resized.astype(np.float32) / 255.0
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD  # broadcast over H,W,C
    chw = np.transpose(arr, (2, 0, 1))  # HWC -> CHW
    return np.expand_dims(chw, 0).astype(np.float32)  # NCHW


class Embedder:
    """Wraps an ONNX DINOv2 session for CPU inference."""

    def __init__(self, onnx_path: str):
        opts = ort.SessionOptions()
        opts.log_severity_level = 3  # errors only; suppress GPU device-discovery warnings
        self._session = ort.InferenceSession(
            onnx_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        tensor = preprocess_to_tensor(crop_bgr)
        (out,) = self._session.run(None, {self._input_name: tensor})
        out = np.asarray(out, dtype=np.float32)
        if out.ndim == 3:          # (1, N_tokens, 384) -> mean-pool patch tokens
            vec = out[0].mean(axis=0)
        else:                      # (1, 384) pooled output
            vec = out.reshape(-1)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)


_MARGIN_SCALE = 0.05  # cosine-distance gap at which the margin bonus saturates


def embed_match(embedder: "Embedder", qdrant, crop_bgr: np.ndarray) -> MethodResult:
    """Method A shortlist score.

    score = top_sim * (0.5 + 0.5 * min(margin_to_second_distinct / 0.05, 1.0))
    where margin is measured in cosine DISTANCE (1 - sim) between the top hit and
    the first shortlist entry belonging to a DIFFERENT card.
    """
    query = embedder.embed(crop_bgr).tolist()
    results = qdrant.search(
        collection_name=QDRANT_COLLECTION, query_vector=query, limit=5
    )
    if not results:
        return MethodResult(method="a", card_ref_id=None, score=0.0)

    top = results[0]
    top_id = top.payload["card_ref_id"]
    top_sim = float(top.score)
    top_dist = 1.0 - top_sim

    second_dist = None
    for point in results[1:]:
        if point.payload["card_ref_id"] != top_id:
            second_dist = 1.0 - float(point.score)
            break

    margin = 0.0 if second_dist is None else (second_dist - top_dist)
    factor = 0.5 + 0.5 * min(max(margin, 0.0) / _MARGIN_SCALE, 1.0)
    score = top_sim * factor
    return MethodResult(method="a", card_ref_id=top_id, score=score)
