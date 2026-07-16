"""One-time DINOv2 embedding index build (GPU-optional export, CPU-only runtime).

Runtime inference (Embedder, embed_match) is CPU ONNX everywhere, mirroring the
VPS. This script is the only place torch is used, and only for the export step
that turns the DINOv2 ViT-S/14 checkpoint into an int8-quantized ONNX graph.
Install build deps with: uv sync --extra build.

Flow:
  1. If worker/models/dinov2_vits14_int8.onnx is missing, export it from
     torch.hub DINOv2, then int8-quantize with onnxruntime.quantization.
  2. For every card_refs row with a local scan at data/refs/{id}.png, embed
     through the SAME preprocessing as user crops (WebP q80 round-trip for
     codec parity, design A4) and upsert to the Qdrant 'card_refs' collection
     (vector size 384, cosine distance) in batches of 256.

Disaster-recovery posture for Method A (design A10): the Qdrant collection is
fully reconstructable via this script — `bws run -- uv run python
scripts/build_embed_index.py --recreate` from `worker/data/refs/` — so M1
keeps no separate Qdrant backup.

Usage:
  bws run -- uv run --extra build python scripts/build_embed_index.py \
      [--recreate] [--sets sv4,sv5] [--limit N]
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import cv2
import numpy as np
from qdrant_client import QdrantClient, models as qmodels

# Allow `python scripts/build_embed_index.py` from the worker/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notbulk.config import load_config
from notbulk.db import get_pool
from notbulk.embed import Embedder, QDRANT_COLLECTION, preprocess_to_tensor
from notbulk.preprocess import webp_roundtrip

# Stable namespace so a card_ref_id always maps to the same Qdrant point id
# (uuid5 is deterministic -> idempotent re-upserts, no duplicate points).
CARD_REF_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "notbulk/card_refs")

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
ONNX_PATH = MODELS_DIR / "dinov2_vits14_int8.onnx"
VECTOR_SIZE = 384
UPSERT_BATCH = 256


def preprocess_ref(crop_bgr: np.ndarray, webp_quality: int = 80) -> np.ndarray:
    """Reference preprocessing == query preprocessing: WebP round-trip for codec
    parity (design A4), then the standard embed tensor."""
    parity = webp_roundtrip(crop_bgr, quality=webp_quality)
    return preprocess_to_tensor(parity)


def build_point(embedder, qdrant_models, card_ref_id: str, crop_bgr: np.ndarray):
    """Build a Qdrant PointStruct: deterministic uuid5 id, 384-d vector, payload.

    Note: embedder.embed() applies its own preprocessing (which includes the
    resize/normalize but NOT the webp round-trip); callers that need codec
    parity with the query path should pass a webp-roundtripped crop in.
    """
    vector = embedder.embed(crop_bgr).tolist()
    point_id = str(uuid.uuid5(CARD_REF_NAMESPACE, card_ref_id))
    return qdrant_models.PointStruct(
        id=point_id, vector=vector, payload={"card_ref_id": card_ref_id}
    )


def export_onnx(onnx_path: Path) -> None:
    """One-time: DINOv2 ViT-S/14 -> ONNX (dynamic batch, opset 17) -> int8.

    Heavy imports are LOCAL so importing this module (e.g. in tests) never
    pulls in torch or onnx's export machinery.
    """
    import torch  # local import: only needed for the one-time export
    from onnxruntime.quantization import QuantType, quantize_dynamic

    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    fp32_path = onnx_path.with_name("dinov2_vits14_fp32.onnx")

    model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
    model.eval()
    dummy = torch.randn(1, 3, 224, 224)
    torch.onnx.export(
        model,
        dummy,
        str(fp32_path),
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17,
    )
    quantize_dynamic(str(fp32_path), str(onnx_path), weight_type=QuantType.QInt8)
    fp32_path.unlink(missing_ok=True)
    print(f"[build_embed_index] exported int8 ONNX -> {onnx_path}")


def _select_card_ids(cur, sets: list[str] | None, limit: int | None) -> list[str]:
    clauses, params = [], []
    if sets:
        clauses.append("set_id = ANY(%s)")
        params.append(sets)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT id FROM card_refs {where} ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def _iter_ref_images(card_ids: list[str]):
    """Yield (card_ref_id, bgr_image) for card_ids with a local scan on disk."""
    for card_ref_id in card_ids:
        path = REFS_DIR / f"{card_ref_id}.png"
        if not path.exists():
            print(f"[build_embed_index] skip {card_ref_id}: no local scan")
            continue
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            print(f"[build_embed_index] skip {card_ref_id}: unreadable image")
            continue
        yield card_ref_id, img


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the DINOv2 Qdrant index")
    parser.add_argument("--recreate", action="store_true",
                        help="drop and recreate the collection before upsert")
    parser.add_argument("--sets", default=None,
                        help="comma-separated set_ids to include, e.g. sv4,sv5")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of refs (for smoke runs)")
    args = parser.parse_args()

    cfg = load_config()
    webp_quality = int(cfg["crop"]["webp_quality"])

    if not ONNX_PATH.exists():
        print("[build_embed_index] ONNX model missing; exporting (one-time, needs --extra build)...")
        export_onnx(ONNX_PATH)

    embedder = Embedder(str(ONNX_PATH))
    client = QdrantClient(url=cfg["qdrant"]["url"])

    if args.recreate:
        client.recreate_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=qmodels.VectorParams(
                size=VECTOR_SIZE, distance=qmodels.Distance.COSINE
            ),
        )
        print(f"[build_embed_index] recreated collection {QDRANT_COLLECTION}")

    sets = args.sets.split(",") if args.sets else None
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            card_ids = _select_card_ids(cur, sets, args.limit)

    print(f"[build_embed_index] {len(card_ids)} candidate cards")

    batch: list = []
    total = 0
    for card_ref_id, img in _iter_ref_images(card_ids):
        parity_crop = webp_roundtrip(img, quality=webp_quality)
        batch.append(build_point(embedder, qmodels, card_ref_id, parity_crop))
        if len(batch) >= UPSERT_BATCH:
            client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
            total += len(batch)
            print(f"[build_embed_index] upserted {total} points...")
            batch = []
    if batch:
        client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        total += len(batch)

    print(f"[build_embed_index] done: {total} points in {QDRANT_COLLECTION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
