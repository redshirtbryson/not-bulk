import json
import sys
from pathlib import Path

# eval/ is not a package; import the sibling module directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import regression  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
from notbulk.types import Identification, MethodResult  # noqa: E402


def _cfg():
    return {"cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40}}


def _ident(card_ref_id, stage, conf=90, methods=None):
    return Identification(
        card_ref_id=card_ref_id, confidence=conf, accepted_stage=stage,
        rotation=0, methods=methods or [], candidates=[],
    )


def test_score_photo_classifies_outcomes():
    manifest_photo = {
        "file": "IMG_001.jpg",
        "scenario": "clean",
        "cards": [
            {"card_ref_id": "sv4-1", "finish": "normal", "notes": ""},   # correct accept
            {"card_ref_id": "sv4-2", "finish": "holofoil", "notes": ""}, # WRONG accept
            {"card_ref_id": "sv4-3", "finish": "normal", "notes": ""},   # validation
            {"card_ref_id": "sv4-4", "finish": "normal", "notes": ""},   # unreadable
            {"card_ref_id": "sv4-5", "finish": "normal", "notes": ""},   # missed detection
        ],
    }
    idents = [
        _ident("sv4-1", "h"),                 # correct
        _ident("sv4-99", "multi"),            # WRONG (auto-accepted, wrong id)
        _ident(None, "validation", conf=55),  # validation
        _ident(None, "unreadable", conf=0),   # unreadable
        # index 4 missing -> missed_detection
    ]
    rows = regression.score_photo(manifest_photo, idents, _cfg())
    outcomes = [r["outcome"] for r in rows]
    assert outcomes == [
        "auto_accepted_correct",
        "auto_accepted_WRONG",
        "sent_to_validation",
        "unreadable",
        "missed_detection",
    ]
    # scenario + finish carried through for splitting
    assert rows[1]["scenario"] == "clean"
    assert rows[1]["finish"] == "holofoil"
    assert rows[1]["expected"] == "sv4-2"
    assert rows[1]["got"] == "sv4-99"


def test_aggregate_metrics_and_splits():
    manifest_photo = {
        "file": "IMG.jpg", "scenario": "holo",
        "cards": [
            {"card_ref_id": "sv4-1", "finish": "holofoil"},
            {"card_ref_id": "sv4-2", "finish": "normal"},
        ],
    }
    idents = [_ident("sv4-1", "h"), _ident("sv4-2", "multi")]
    rows = regression.score_photo(manifest_photo, idents, _cfg())
    metrics = regression.aggregate(rows, llm_calls=0)
    assert metrics["total_cards"] == 2
    assert metrics["auto_accept_rate"] == 1.0
    assert metrics["hash_tier_hit_rate"] == 0.5      # 1 of 2 at stage 'h'
    assert metrics["wrong_auto_accepts"]["count"] == 0
    assert metrics["by_scenario"]["holo"]["auto_accepted"] == 2
    assert metrics["by_finish"]["holofoil"]["total"] == 1


def test_check_regression_hard_fails_on_wrong_accept():
    manifest_photo = {"file": "IMG.jpg", "scenario": "clean",
                      "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}]}
    idents = [_ident("sv4-999", "h")]  # wrong id, auto-accepted
    metrics = regression.aggregate(regression.score_photo(manifest_photo, idents, _cfg()))
    passed, reason = regression.check_regression(metrics, {"auto_accept_rate": 0.0})
    assert passed is False
    assert "WRONG AUTO-ACCEPT" in reason


def test_check_regression_fails_on_rate_drop():
    metrics = {"wrong_auto_accepts": {"count": 0, "cards": []}, "auto_accept_rate": 0.80}
    passed, reason = regression.check_regression(metrics, {"auto_accept_rate": 0.90})
    assert passed is False
    assert "regression" in reason


def test_check_regression_passes_within_tolerance():
    metrics = {"wrong_auto_accepts": {"count": 0, "cards": []}, "auto_accept_rate": 0.895}
    passed, _ = regression.check_regression(metrics, {"auto_accept_rate": 0.90})
    assert passed is True


def test_count_llm_calls():
    idents = [
        _ident("sv4-1", "llm", methods=[MethodResult("h", None, 0.1),
                                        MethodResult("c", "sv4-1", 0.9)]),
        _ident("sv4-2", "h", methods=[MethodResult("h", "sv4-2", 0.99)]),
    ]
    assert regression._count_llm_calls(idents) == 1


def _make_deps_stub():
    class _Deps:  # duck-typed CascadeDeps; regression.main never inspects fields
        pass
    return _Deps()


def test_main_smoke_pass(tmp_path, monkeypatch):
    manifest = {"photos": [{
        "file": "IMG_001.jpg", "scenario": "clean",
        "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}],
    }]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"auto_accept_rate": 0.0, "hash_tier_hit_rate": 0.0}))
    last_run_path = tmp_path / "last_run.json"

    monkeypatch.setattr(regression, "_MANIFEST", manifest_path)
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_LAST_RUN", last_run_path)
    monkeypatch.setattr(regression, "load_config", lambda path=None: {
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    })
    monkeypatch.setattr(regression, "_load_pipeline", lambda cfg: _make_deps_stub())

    class _Det:
        def __init__(self, i):
            import numpy as np
            self.crop = np.zeros((1024, 734, 3), dtype="uint8")
            self.crop_index = i

    monkeypatch.setattr(regression, "_read_photo", lambda path: object())
    monkeypatch.setattr(regression, "_detect", lambda photo, cfg: [_Det(0)])
    monkeypatch.setattr(regression, "_identify",
                        lambda crop, deps, cfg: _ident("sv4-1", "h"))

    code = regression.main([])
    assert code == 0
    assert last_run_path.exists()


def test_main_smoke_wrong_accept_exit_one(tmp_path, monkeypatch):
    manifest = {"photos": [{
        "file": "IMG_001.jpg", "scenario": "clean",
        "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}],
    }]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"auto_accept_rate": 0.0}))
    monkeypatch.setattr(regression, "_MANIFEST", manifest_path)
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_LAST_RUN", tmp_path / "last_run.json")
    monkeypatch.setattr(regression, "load_config", lambda path=None: {
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx"}, "qdrant": {"url": "x"}})
    monkeypatch.setattr(regression, "_load_pipeline", lambda cfg: _make_deps_stub())

    class _Det:
        def __init__(self, i):
            import numpy as np
            self.crop = np.zeros((1024, 734, 3), dtype="uint8")
            self.crop_index = i

    monkeypatch.setattr(regression, "_read_photo", lambda path: object())
    monkeypatch.setattr(regression, "_detect", lambda photo, cfg: [_Det(0)])
    monkeypatch.setattr(regression, "_identify",
                        lambda crop, deps, cfg: _ident("sv4-WRONG", "h"))
    code = regression.main([])
    assert code == 1
