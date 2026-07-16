import numpy as np
import pytest

from notbulk.embed import preprocess_to_tensor


def test_preprocess_to_tensor_shape_and_dtype():
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)  # BGR 734x1024 (w x h)
    t = preprocess_to_tensor(crop)
    assert t.shape == (1, 3, 224, 224)
    assert t.dtype == np.float32


def test_preprocess_to_tensor_imagenet_normalized():
    # A mid-gray 128/255 ~= 0.502 input, after ImageNet normalization, must land
    # near (0.502 - mean) / std per channel. Channels are RGB order after BGR->RGB.
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)
    t = preprocess_to_tensor(crop)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    expected = (128.0 / 255.0 - mean) / std  # per-channel scalar (constant image)
    got = t[0].mean(axis=(1, 2))  # mean over H,W -> per-channel
    np.testing.assert_allclose(got, expected, rtol=1e-4, atol=1e-4)


onnx = pytest.importorskip("onnx")  # tiny ONNX build needs the onnx package
import onnxruntime  # noqa: E402  (runtime dep, always present)

from notbulk.embed import Embedder  # noqa: E402


@pytest.fixture
def tiny_onnx_path(tmp_path):
    """Build a valid, tiny ONNX model: (1,3,224,224)
    -> GlobalAveragePool -> (1,3,1,1) -> Reshape -> (1,3) -> MatMul(3,384) -> (1,384).

    Pooling to 3 channels before the MatMul keeps the weight matrix a few KB
    instead of allocating a (150528, 384) ~231MB initializer. This exercises the
    rank-2 (pooled) output branch of Embedder.embed.
    """
    from onnx import TensorProto, helper, numpy_helper

    out_feats = 384

    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 224, 224])
    out = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, out_feats])

    # Reshape target as an initializer constant [1, 3].
    shape_init = numpy_helper.from_array(
        np.array([1, 3], dtype=np.int64), name="reshape_shape"
    )
    rng = np.random.default_rng(0)
    weight = numpy_helper.from_array(
        rng.standard_normal((3, out_feats)).astype(np.float32), name="W"
    )

    pool_node = helper.make_node("GlobalAveragePool", ["input"], ["pooled"])
    reshape_node = helper.make_node("Reshape", ["pooled", "reshape_shape"], ["flat"])
    matmul_node = helper.make_node("MatMul", ["flat", "W"], ["output"])

    graph = helper.make_graph(
        [pool_node, reshape_node, matmul_node],
        "tiny_embed",
        [inp],
        [out],
        initializer=[shape_init, weight],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9  # compatible with onnxruntime + opset 17
    onnx.checker.check_model(model)

    path = tmp_path / "tiny.onnx"
    onnx.save(model, str(path))
    return str(path)


def test_embedder_output_is_l2_normalized_384(tiny_onnx_path):
    emb = Embedder(tiny_onnx_path)
    crop = np.full((1024, 734, 3), 90, dtype=np.uint8)
    vec = emb.embed(crop)
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vec), 1.0, rtol=1e-5, atol=1e-5)


@pytest.fixture
def token_onnx_path(tmp_path):
    """Build an ONNX model emitting a rank-3 token sequence (1, 16, 384) to
    exercise the mean-pool branch of Embedder.embed.

    Same GlobalAveragePool trick as tiny_onnx_path keeps the MatMul weight a
    few KB (3, 16*384) instead of (150528, 6144) ~3.7GB.
    """
    from onnx import TensorProto, helper, numpy_helper

    n_tokens, out_feats = 16, 384
    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 224, 224])
    out = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, n_tokens, out_feats])

    shape_init = numpy_helper.from_array(
        np.array([1, 3], dtype=np.int64), name="reshape_shape"
    )
    rng = np.random.default_rng(1)
    weight = numpy_helper.from_array(
        rng.standard_normal((3, n_tokens * out_feats)).astype(np.float32),
        name="W",
    )
    out_shape = numpy_helper.from_array(
        np.array([1, n_tokens, out_feats], dtype=np.int64), name="out_shape"
    )
    pool_node = helper.make_node("GlobalAveragePool", ["input"], ["pooled"])
    reshape_in = helper.make_node("Reshape", ["pooled", "reshape_shape"], ["flat"])
    matmul = helper.make_node("MatMul", ["flat", "W"], ["wide"])
    reshape_out = helper.make_node("Reshape", ["wide", "out_shape"], ["output"])
    graph = helper.make_graph(
        [pool_node, reshape_in, matmul, reshape_out],
        "tiny_tokens",
        [inp],
        [out],
        initializer=[shape_init, weight, out_shape],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    path = tmp_path / "tokens.onnx"
    onnx.save(model, str(path))
    return str(path)


def test_embedder_mean_pools_token_sequence(token_onnx_path):
    emb = Embedder(token_onnx_path)
    crop = np.full((1024, 734, 3), 90, dtype=np.uint8)
    vec = emb.embed(crop)
    assert vec.shape == (384,)
    np.testing.assert_allclose(np.linalg.norm(vec), 1.0, rtol=1e-5, atol=1e-5)


from dataclasses import dataclass

from notbulk.embed import embed_match


@dataclass
class _StubPoint:
    """Mimics qdrant_client.models.ScoredPoint (only fields we read)."""
    score: float
    payload: dict


class _StubQdrant:
    """Stub exposing .search(collection_name, query_vector, limit) -> list."""
    def __init__(self, points):
        self._points = points
        self.calls = []

    def search(self, collection_name, query_vector, limit):
        self.calls.append((collection_name, limit))
        return self._points[:limit]


class _StubEmbedder:
    def embed(self, crop_bgr):
        return np.ones(384, dtype=np.float32) / np.sqrt(384)


def test_embed_match_margin_full_bonus():
    # Top sim 0.90; second DISTINCT card is >=0.05 cosine-distance away.
    # cosine distance = 1 - sim. top dist=0.10, second card sim=0.80 -> dist=0.20,
    # margin_to_second_distinct = 0.20 - 0.10 = 0.10 >= 0.05 -> full bonus factor 1.0.
    # score = 0.90 * (0.5 + 0.5 * min(0.10/0.05, 1.0)) = 0.90 * 1.0 = 0.90
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.80, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.method == "a"
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.90) < 1e-6


def test_embed_match_margin_partial():
    # top sim 0.90 (dist 0.10); second distinct sim 0.88 (dist 0.12);
    # margin = 0.02; factor = 0.5 + 0.5 * min(0.02/0.05,1.0) = 0.5 + 0.5*0.4 = 0.70
    # score = 0.90 * 0.70 = 0.63
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.88, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.63) < 1e-6


def test_embed_match_skips_same_card_for_margin():
    # Second point is the SAME card as the top; margin must be measured against
    # the first DISTINCT card (sv4-2 at sim 0.70 -> dist 0.30, margin 0.20 -> full).
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.89, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.70, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.90) < 1e-6


def test_embed_match_no_results_returns_none():
    r = embed_match(_StubEmbedder(), _StubQdrant([]), np.zeros((1024, 734, 3), np.uint8))
    assert r.method == "a"
    assert r.card_ref_id is None
    assert r.score == 0.0


def test_embed_match_single_distinct_card_gets_min_factor():
    # Only one distinct card in the shortlist -> no second distinct -> margin 0 ->
    # factor = 0.5 + 0.5 * min(0/0.05,1.0) = 0.5. score = 0.90 * 0.5 = 0.45
    points = [_StubPoint(0.90, {"card_ref_id": "sv4-1"})]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.45) < 1e-6
