import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from notbulk import cli
from notbulk.types import Identification, MethodResult


def test_resolve_config_walks_up(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "worker" / "sub").mkdir(parents=True)
    cfg_file = root / "config.yaml"
    cfg_file.write_text("crop: {width: 734}\n")
    monkeypatch.chdir(root / "worker" / "sub")
    found = cli.resolve_config_path(None)
    assert Path(found).resolve() == cfg_file.resolve()


def test_resolve_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        cli.resolve_config_path(None)


def _write_synthetic_jpg(path: Path):
    img = np.full((200, 150, 3), 200, dtype=np.uint8)
    cv2.imwrite(str(path), img)


class _FakeDetection:
    def __init__(self, crop_index):
        self.crop = np.full((1024, 734, 3), 127, dtype=np.uint8)
        self.crop_index = crop_index
        self.sharpness = 100.0
        self.quad = np.zeros((4, 2), dtype="float32")


class _FakePool:
    # Batched card_refs lookup: `id = ANY(%s)` gets a list param and fetchall.
    # Records every query so tests can assert exactly one lookup per run (no N+1).
    _KNOWN = {"sv4-7": "Charizard"}

    def __init__(self):
        self.queries = []

    def connection(self):
        pool = self

        class _Conn:
            def execute(self, sql, params):
                pool.queries.append((sql, params))
                ids = list(params[0]) if params else []

                class _Cur:
                    def fetchall(self_inner):
                        return [(i, pool._KNOWN[i]) for i in ids if i in pool._KNOWN]

                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Conn()


class _FakeIndex:
    # HashIndex-like stub: `_build_deps` only calls `len(index)` to detect an
    # unbuilt index. `n=0` triggers the "ref_hashes is empty" SystemExit.
    def __init__(self, n=10):
        self._n = n

    def __len__(self):
        return self._n


def test_main_writes_json_and_exit_zero(tmp_path, monkeypatch):
    photo = tmp_path / "IMG_001.jpg"
    _write_synthetic_jpg(photo)
    out = tmp_path / "out.json"

    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "worker/models/does_not_exist.onnx",
                   "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    fake_pool = _FakePool()
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: fake_pool)
    monkeypatch.setattr(cli.HashIndex, "load", classmethod(lambda cls, pool: _FakeIndex()))
    monkeypatch.setattr(cli.detect_mod, "detect_cards",
                        lambda photo_img, cfg: [_FakeDetection(0)])
    monkeypatch.setattr(
        cli.cascade_mod, "identify_crop",
        lambda crop, deps, cfg: Identification(
            card_ref_id="sv4-7", confidence=93, accepted_stage="h", rotation=0,
            methods=[MethodResult("h", "sv4-7", 0.98)], candidates=["sv4-7"]),
    )
    # onnx file absent -> Embedder skipped; assert it is never constructed.
    monkeypatch.setattr(cli, "Embedder",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Embedder built")))
    # --no-llm must win over a set key: patch the real constructor to raise so
    # the test fails loudly if the gate ever regresses.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-test")
    import anthropic as anthropic_mod
    monkeypatch.setattr(
        anthropic_mod, "Anthropic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Anthropic built under --no-llm")))

    code = cli.main([str(photo), "--json", str(out), "--no-llm"])
    assert code == 0
    # Exactly one card_refs query for the whole batch (no per-card N+1).
    assert len(fake_pool.queries) == 1
    sql, params = fake_pool.queries[0]
    assert "ANY" in sql and list(params[0]) == ["sv4-7"]
    data = json.loads(out.read_text())
    assert data == {
        "photos": [
            {
                "file": str(photo),
                "cards": [
                    {
                        "crop_index": 0,
                        "card_ref_id": "sv4-7",
                        "name": "Charizard",
                        "confidence": 93,
                        "accepted_stage": "h",
                        "rotation": 0,
                        "methods": [{"method": "h", "card_ref_id": "sv4-7", "score": 0.98}],
                        "candidates": ["sv4-7"],
                    }
                ],
            }
        ]
    }


def test_main_llm_enabled_constructs_anthropic(tmp_path, monkeypatch):
    # Inverse of the --no-llm gate: key set, no --no-llm -> constructor IS called.
    photo = tmp_path / "IMG_002.jpg"
    _write_synthetic_jpg(photo)

    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx", "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(cli.HashIndex, "load", classmethod(lambda cls, pool: _FakeIndex()))
    monkeypatch.setattr(cli.detect_mod, "detect_cards", lambda photo_img, cfg: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-test")

    llm_stub = object()
    constructed = []
    import anthropic as anthropic_mod
    monkeypatch.setattr(
        anthropic_mod, "Anthropic",
        lambda *a, **k: (constructed.append((a, k)), llm_stub)[1])

    captured_deps = []
    real_build_deps = cli._build_deps

    def _spy_build_deps(cfg_arg, pool, *, no_llm):
        deps = real_build_deps(cfg_arg, pool, no_llm=no_llm)
        captured_deps.append(deps)
        return deps

    monkeypatch.setattr(cli, "_build_deps", _spy_build_deps)

    code = cli.main([str(photo)])
    assert code == 0
    assert len(constructed) == 1
    assert captured_deps[0].anthropic is llm_stub


def test_main_no_readable_photos_exit_one(tmp_path, monkeypatch):
    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx", "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(cli.HashIndex, "load", classmethod(lambda cls, pool: _FakeIndex()))
    monkeypatch.setattr(cli, "Embedder", lambda *a, **k: None)
    # Non-existent file -> imread returns None -> skipped -> zero readable.
    code = cli.main([str(tmp_path / "missing.jpg"), "--no-llm"])
    assert code == 1


def test_main_empty_hash_index_exits_with_build_hint(tmp_path, monkeypatch):
    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx", "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    # Zero-length index -> _build_deps must raise before any model is built.
    monkeypatch.setattr(cli.HashIndex, "load",
                        classmethod(lambda cls, pool: _FakeIndex(n=0)))
    with pytest.raises(SystemExit, match="ref_hashes is empty"):
        cli.main([str(tmp_path / "any.jpg"), "--no-llm"])
