from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2

from . import cascade as cascade_mod
from . import detect as detect_mod
from .config import load_config
from .db import get_pool
from .embed import Embedder
from .hash_index import HashIndex
from .ocr import OcrReader
from .types import Identification


def resolve_config_path(explicit: str | None) -> str:
    """--config value if given; otherwise walk up from cwd to find config.yaml."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return str(p)
    cur = Path.cwd()
    for directory in (cur, *cur.parents):
        candidate = directory / "config.yaml"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "config.yaml not found in cwd or any parent directory; pass --config"
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="notbulk-scan", description="Scan card photos.")
    p.add_argument("photos", nargs="+", metavar="PHOTO", help="image file paths")
    p.add_argument("--json", dest="json_path", default=None, help="write JSON output here")
    p.add_argument("--no-llm", action="store_true", help="disable Method C (Anthropic)")
    p.add_argument("--config", default=None, help="path to config.yaml (else walk up)")
    return p


def _card_names(pool, card_ref_ids: list[str]) -> dict[str, str]:
    """One batched card_refs name lookup for the run's distinct accepted ids.

    Candidates in the JSON output are bare id strings (unnamed), so only the
    primary card_ref_id per card needs a name. Ids missing from card_refs are
    simply absent from the dict; callers .get() them as None."""
    if not card_ref_ids:
        return {}
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, name FROM card_refs WHERE id = ANY(%s)", (card_ref_ids,)
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _build_deps(cfg: dict, pool, *, no_llm: bool):
    hash_index = HashIndex.load(pool)
    if len(hash_index) == 0:
        raise SystemExit(
            "ref_hashes is empty — run scripts/build_hash_index.py first"
        )

    onnx_path = cfg["models"]["embedding_onnx"]
    embedder = None
    qdrant = None
    if Path(onnx_path).is_file():
        embedder = Embedder(onnx_path)
        # qdrant-client is imported lazily so tests that monkeypatch Embedder/HashIndex
        # never require a running Qdrant. Wired only when Method A is active.
        from qdrant_client import QdrantClient

        qdrant = QdrantClient(url=cfg["qdrant"]["url"])
    else:
        print(f"warning: ONNX model not found at {onnx_path}; skipping Method A",
              file=sys.stderr)

    ocr_reader = OcrReader()  # lazy: PaddleOCR loads on first read_regions call

    anthropic_client = None
    if not no_llm and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic  # imported lazily so tests never touch it when --no-llm

        anthropic_client = anthropic.Anthropic()

    return cascade_mod.CascadeDeps(
        hash_index=hash_index,
        embedder=embedder,
        qdrant=qdrant,  # Method A client, constructed alongside the embedder
        ocr_reader=ocr_reader,
        anthropic=anthropic_client,
        pool=pool,
    )


def _identification_to_dict(det, ident: Identification, names: dict[str, str]) -> dict:
    return {
        "crop_index": det.crop_index,
        "card_ref_id": ident.card_ref_id,
        "name": names.get(ident.card_ref_id) if ident.card_ref_id else None,
        "confidence": ident.confidence,
        "accepted_stage": ident.accepted_stage,
        "rotation": ident.rotation,
        "methods": [
            {"method": m.method, "card_ref_id": m.card_ref_id, "score": m.score}
            for m in ident.methods
        ],
        "candidates": list(ident.candidates),
    }


def main(argv: list[str] | None = None) -> int:
    # Pool lifetime: intentionally never closed here — including the SystemExit
    # path inside _build_deps. This is a short-lived CLI process; connections
    # are reclaimed at process exit (psycopg_pool registers its own atexit
    # cleanup), so explicit close/finally ceremony buys nothing.
    args = _build_parser().parse_args(argv)
    cfg_path = resolve_config_path(args.config)
    cfg = load_config(cfg_path)
    pool = get_pool()
    deps = _build_deps(cfg, pool, no_llm=args.no_llm)

    # Pass 1: detect + identify everything, deferring name lookups so the whole
    # batch resolves with a single card_refs query afterwards (no N+1).
    results: list[tuple[str, list[tuple[object, Identification]]]] = []
    readable = 0

    for photo_path in args.photos:
        img = cv2.imread(photo_path)
        if img is None:
            print(f"warning: could not read {photo_path}; skipping", file=sys.stderr)
            continue
        readable += 1
        pairs: list[tuple[object, Identification]] = []
        for det in detect_mod.detect_cards(img, cfg):
            ident = cascade_mod.identify_crop(det.crop, deps, cfg)
            pairs.append((det, ident))
        results.append((photo_path, pairs))

    distinct_ids = sorted({
        ident.card_ref_id
        for _, pairs in results
        for _, ident in pairs
        if ident.card_ref_id is not None
    })
    names = _card_names(pool, distinct_ids)

    photos_out: list[dict] = [
        {
            "file": photo_path,
            "cards": [
                _identification_to_dict(det, ident, names) for det, ident in pairs
            ],
        }
        for photo_path, pairs in results
    ]

    _print_table(photos_out)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"photos": photos_out}, indent=2)
        )

    return 0 if readable > 0 else 1


def _print_table(photos_out: list[dict]) -> None:
    header = f"{'photo':<24} {'idx':>3} {'card_ref_id':<12} {'name':<20} {'conf':>4} {'stage':<10} {'rot':>3}"
    print(header)
    print("-" * len(header))
    for photo in photos_out:
        fname = Path(photo["file"]).name
        if not photo["cards"]:
            print(f"{fname:<24} {'-':>3} {'(no cards)':<12}")
            continue
        for c in photo["cards"]:
            print(
                f"{fname:<24} {c['crop_index']:>3} "
                f"{(c['card_ref_id'] or '-'):<12} {(c['name'] or '-'):<20} "
                f"{c['confidence']:>4} {c['accepted_stage']:<10} {c['rotation']:>3}"
            )
