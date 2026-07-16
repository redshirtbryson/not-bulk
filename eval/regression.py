#!/usr/bin/env python3
"""NotBulk accuracy regression harness (spec §8).

Run from the worker env so notbulk imports resolve:
    cd worker && uv run python ../eval/regression.py [--update-baseline]

Exit codes: 0 pass, 1 wrong auto-accept OR auto-accept-rate regression,
2 config/data error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _REPO_ROOT / "worker"
if str(_WORKER) not in sys.path:
    sys.path.insert(0, str(_WORKER))

_ACCEPT_STAGES = {"h", "multi", "llm"}
_MANIFEST = _REPO_ROOT / "ground-truth" / "manifest.json"
_BASELINE = _REPO_ROOT / "eval" / "baseline.json"
_LAST_RUN = _REPO_ROOT / "eval" / "last_run.json"


def score_photo(manifest_photo: dict, idents: list, cfg: dict) -> list[dict]:
    """Match detected identifications to manifest cards by crop_index order and
    classify each into one outcome.

    Caveat (spec §4.1): matching is purely positional. A missed or extra
    detection shifts alignment for every subsequent card in that photo. This
    is acceptable given the spec's stable-ordering assumption for detection;
    revisit with IoU-based alignment only if eval surfaces spurious
    wrong-accepts traceable to misalignment rather than real model error.
    """
    scenario = manifest_photo.get("scenario", "unknown")
    manifest_cards = manifest_photo.get("cards", [])
    rows: list[dict] = []
    for i, card in enumerate(manifest_cards):
        expected = card["card_ref_id"]
        finish = card.get("finish", "unknown")
        base = {
            "file": manifest_photo.get("file", ""),
            "scenario": scenario,
            "finish": finish,
            "crop_index": i,
            "expected": expected,
        }
        if i >= len(idents):
            rows.append({**base, "outcome": "missed_detection", "got": None,
                         "accepted_stage": None})
            continue
        ident = idents[i]
        stage = ident.accepted_stage
        got = ident.card_ref_id
        if stage == "unreadable":
            outcome = "unreadable"
        elif stage in _ACCEPT_STAGES:
            outcome = "auto_accepted_correct" if got == expected else "auto_accepted_WRONG"
        else:  # 'validation'
            outcome = "sent_to_validation"
        rows.append({**base, "outcome": outcome, "got": got, "accepted_stage": stage})
    return rows


def _count_llm_calls(idents: list) -> int:
    total = 0
    for ident in idents:
        total += sum(1 for m in ident.methods if m.method == "c")
    return total


def aggregate(rows: list[dict], llm_calls: int = 0) -> dict:
    # auto_accept_rate denominator is ALL manifest cards (including missed,
    # unreadable, and validation) — the 90% soft target is read against it.
    total = len(rows)
    accepted = [r for r in rows if r["outcome"].startswith("auto_accepted")]
    wrong = [r for r in rows if r["outcome"] == "auto_accepted_WRONG"]
    hash_hits = [r for r in rows if r.get("accepted_stage") == "h"]

    def _rate(n: int) -> float:
        return round(n / total, 4) if total else 0.0

    stage_dist: dict[str, int] = {}
    for r in rows:
        key = r.get("accepted_stage") or "none"
        stage_dist[key] = stage_dist.get(key, 0) + 1

    def _split(field: str) -> dict:
        out: dict[str, dict] = {}
        for r in rows:
            k = r.get(field, "unknown")
            bucket = out.setdefault(k, {"total": 0, "auto_accepted": 0, "wrong": 0})
            bucket["total"] += 1
            if r["outcome"].startswith("auto_accepted"):
                bucket["auto_accepted"] += 1
            if r["outcome"] == "auto_accepted_WRONG":
                bucket["wrong"] += 1
        return out

    return {
        "total_cards": total,
        "wrong_auto_accepts": {"count": len(wrong), "cards": wrong},
        "auto_accept_rate": _rate(len(accepted)),
        "hash_tier_hit_rate": _rate(len(hash_hits)),
        "exit_stage_distribution": stage_dist,
        "llm_calls": llm_calls,
        "by_scenario": _split("scenario"),
        "by_finish": _split("finish"),
    }


def check_regression(metrics: dict, baseline: dict) -> tuple[bool, str]:
    """(passed, reason). Fails on any wrong auto-accept (hard) or an
    auto_accept_rate that dropped more than 0.01 below baseline."""
    if metrics["wrong_auto_accepts"]["count"] > 0:
        cards = metrics["wrong_auto_accepts"]["cards"]
        detail = ", ".join(f"{c['file']}#{c['crop_index']} {c['expected']}->{c['got']}"
                            for c in cards)
        return False, f"WRONG AUTO-ACCEPT (hard fail): {detail}"
    base_rate = baseline.get("auto_accept_rate", 0.0)
    if metrics["auto_accept_rate"] < base_rate - 0.01:
        return False, (f"regression: auto_accept_rate {metrics['auto_accept_rate']} "
                       f"< baseline {base_rate} - 0.01")
    return True, "ok"


from notbulk.config import load_config  # noqa: E402
from notbulk.db import get_pool  # noqa: E402


def _load_pipeline(cfg: dict, cfg_path: str):
    """Build CascadeDeps against the real DB/index. Isolated so tests stub it."""
    from notbulk.cascade import CascadeDeps
    from notbulk.embed import Embedder
    from notbulk.hash_index import HashIndex
    from notbulk.ocr import OcrReader

    pool = get_pool()
    hash_index = HashIndex.load(pool)
    if len(hash_index) == 0:
        raise RuntimeError("ref_hashes is empty — run scripts/build_hash_index.py first")
    # Resolved against the config file's parent (repo root), not cwd, so this
    # works whether regression.py is invoked from the repo root or `cd worker`.
    onnx_path = str(Path(cfg_path).resolve().parent / cfg["models"]["embedding_onnx"])
    if Path(onnx_path).is_file():
        embedder = Embedder(onnx_path)
    else:
        embedder = None
        print(f"warning: ONNX model not found at {onnx_path}; skipping Method A",
              file=sys.stderr)
    # qdrant-client is imported lazily so tests stubbing _load_pipeline never need it.
    qdrant = None
    if embedder is not None:
        from qdrant_client import QdrantClient

        qdrant = QdrantClient(url=cfg["qdrant"]["url"])
    return CascadeDeps(
        hash_index=hash_index, embedder=embedder, qdrant=qdrant,
        ocr_reader=OcrReader(), anthropic=None, pool=pool,
    )


def _read_photo(path: str):
    import cv2
    img = cv2.imread(path)
    return img


def _detect(photo, cfg: dict):
    from notbulk.detect import detect_cards
    return detect_cards(photo, cfg)


def _identify(crop, deps, cfg: dict):
    from notbulk.cascade import identify_crop
    return identify_crop(crop, deps, cfg)


def _print_summary(metrics: dict) -> None:
    print("=== NotBulk regression summary ===")
    print(f"cards evaluated : {metrics['total_cards']}")
    print(f"auto-accept rate: {metrics['auto_accept_rate']}")
    print(f"hash-tier hit   : {metrics['hash_tier_hit_rate']}")
    print(f"wrong accepts   : {metrics['wrong_auto_accepts']['count']}")
    print(f"llm calls       : {metrics['llm_calls']}")
    print(f"exit stages     : {metrics['exit_stage_distribution']}")
    print("by scenario     :")
    for k, v in sorted(metrics["by_scenario"].items()):
        print(f"  {k:<10} total={v['total']} accepted={v['auto_accepted']} wrong={v['wrong']}")
    print("by finish       :")
    for k, v in sorted(metrics["by_finish"].items()):
        print(f"  {k:<10} total={v['total']} accepted={v['auto_accepted']} wrong={v['wrong']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="regression", description="NotBulk accuracy gate.")
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite baseline.json from this run")
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(_MANIFEST.read_text())
        baseline = json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"config/data error: {exc}", file=sys.stderr)
        return 2

    try:
        from notbulk.cli import resolve_config_path

        cfg_path = resolve_config_path(None)
        cfg = load_config(cfg_path)
        deps = _load_pipeline(cfg, cfg_path)
    except Exception as exc:  # DB down, index empty, etc.
        print(f"config/data error: {exc}", file=sys.stderr)
        return 2

    all_rows: list[dict] = []
    all_idents: list = []
    for mphoto in manifest.get("photos", []):
        photo_path = str(_REPO_ROOT / "ground-truth" / mphoto["file"])
        img = _read_photo(photo_path)
        if img is None:
            # A missing image maps every manifest card to missed_detection.
            all_rows.extend(score_photo(mphoto, [], cfg))
            continue
        dets = _detect(img, cfg)
        idents = [_identify(d.crop, deps, cfg) for d in dets]
        all_idents.extend(idents)
        all_rows.extend(score_photo(mphoto, idents, cfg))

    metrics = aggregate(all_rows, llm_calls=_count_llm_calls(all_idents))
    _LAST_RUN.write_text(json.dumps({"metrics": metrics, "rows": all_rows}, indent=2))
    _print_summary(metrics)

    # Gate BEFORE honoring --update-baseline: a run containing a wrong
    # auto-accept must never establish or overwrite the baseline (hard
    # invariant, design A1). Deliberately updating the rate downward is
    # legitimate when explicitly requested; a wrong accept never is.
    passed, reason = check_regression(metrics, baseline)

    if args.update_baseline:
        if metrics["wrong_auto_accepts"]["count"] > 0:
            print(f"refusing to update baseline: {reason}", file=sys.stderr)
            return 1
        _BASELINE.write_text(json.dumps({
            "auto_accept_rate": metrics["auto_accept_rate"],
            "hash_tier_hit_rate": metrics["hash_tier_hit_rate"],
        }, indent=2))
        print(f"baseline updated -> {_BASELINE}")
        return 0

    print(f"result: {'PASS' if passed else 'FAIL'} — {reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
