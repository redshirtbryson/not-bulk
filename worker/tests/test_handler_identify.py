"""handle_identify: crop -> identify_crop -> card UPDATE with method columns,
status mapping, finish gating, candidates jsonb, llm_calls usage, and the
single-fire batch completion guard.

identify_crop is monkeypatched to return canned Identifications so no models
run. The DB is a SQL-text-matching scripted pool.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from notbulk.handlers import identify as ih
from notbulk.types import Identification, MethodResult


class ScriptedCursor:
    def __init__(self, responder):
        self._responder = responder
        self._current = []
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responder(sql, params)
        self.rowcount = len(self._current)
        return self

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedPool:
    def __init__(self, responder):
        self.cursor = ScriptedCursor(responder)
        self._conn = ScriptedConn(self.cursor)

    def connection(self):
        return self._conn


class FakeStorage:
    def get(self, key):
        return b"webpbytes"

    @staticmethod
    def crop_key(u, b, c):
        return f"{u}/{b}/crops/{c}.webp"


CFG = {"crop": {"width": 734, "height": 1024, "webp_quality": 80}}


def _responder(finishes, complete_returns=1):
    """finishes: list[str] returned for the card_refs finishes SELECT.
    complete_returns: rows the guarded batch-completion UPDATE returns (1 fires,
    0 = already completed elsewhere)."""
    def responder(sql, params):
        low = " ".join(sql.lower().split())
        if low.startswith("update batches"):
            return [("batch-1",)] * complete_returns
        if "from cards" in low and "join photos" in low:
            # card + photo/batch context: (card_id, batch_id, user_id, crop_key)
            return [("card-1", "batch-1", "user-1", "user-1/batch-1/crops/card-1.webp")]
        if "select finishes from card_refs" in low:
            return [(finishes,)]
        return []
    return responder


def _ident(card_ref_id, stage, methods, candidates):
    return Identification(
        card_ref_id=card_ref_id, confidence=88, accepted_stage=stage,
        rotation=90, methods=methods, candidates=candidates,
    )


def _patch(monkeypatch, ident):
    monkeypatch.setattr(ih, "identify_crop", lambda crop, deps, cfg: ident)
    monkeypatch.setattr(ih, "_deps", lambda cfg, pool: object())
    monkeypatch.setattr(ih.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1024, 734, 3), np.uint8))
    notified = []
    monkeypatch.setattr(ih.jobqueue, "notify_progress",
                        lambda pool, b, e, **ids: notified.append((e, ids)))
    return notified


def _card_update(pool):
    for sql, params in pool.cursor.executed:
        if sql.lower().startswith("update cards"):
            return sql, params
    return None, None


def test_auto_single_finish_keeps_auto_and_sets_finish(monkeypatch):
    ident = _ident("sv4-1", "h",
                   [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    notified = _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)

    sql, params = _card_update(pool)
    assert "status" in sql.lower()
    assert "sv4-1" in params           # card_ref_id
    assert "normal" in params          # single finish set
    assert "auto" in params            # stays auto
    assert any(e == "card_identified" for e, _ in notified)


def test_auto_multi_finish_downgrades_to_validation(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal", "holofoil"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)

    sql, params = _card_update(pool)
    assert "validation" in params      # downgraded (design A1)
    assert "auto" not in params        # not an auto-accept
    # finish_needs_confirmation true is set in the UPDATE
    assert "finish_needs_confirmation" in sql.lower()


def test_validation_path_sets_validation(monkeypatch):
    ident = _ident(None, "validation", [MethodResult("a", "sv4-2", 0.5)], ["sv4-2"])
    _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=[]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    _sql, params = _card_update(pool)
    assert "validation" in params


def test_unreadable_path_sets_unreadable(monkeypatch):
    ident = _ident(None, "unreadable", [], [])
    _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=[]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    _sql, params = _card_update(pool)
    assert "unreadable" in params


def test_candidates_serialized_with_null_scores(monkeypatch):
    ident = _ident("sv4-1", "multi",
                   [MethodResult("h", "sv4-1", 0.9), MethodResult("a", "sv4-1", 0.8)],
                   ["sv4-1", "sv4-9"])
    _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    _sql, params = _card_update(pool)
    # candidates stored as [{"card_ref_id": id, "score": null}, ...]
    cand_json = next(p for p in params if isinstance(p, str) and "card_ref_id" in p)
    assert json.loads(cand_json) == [
        {"card_ref_id": "sv4-1", "score": None},
        {"card_ref_id": "sv4-9", "score": None},
    ]


def test_llm_calls_incremented_when_method_c_present(monkeypatch):
    ident = _ident("sv4-1", "llm",
                   [MethodResult("h", "sv4-1", 0.7), MethodResult("c", "sv4-1", 0.8)],
                   ["sv4-1"])
    _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    llm_updates = [s for s, _ in pool.cursor.executed
                   if "usage" in s.lower() and "llm_calls" in s.lower()]
    assert llm_updates


def test_batch_completion_fires_once(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    notified = _patch(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"], complete_returns=1))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    assert any(e == "batch_complete" for e, _ in notified)

    notified2 = _patch(monkeypatch, ident)
    pool2 = ScriptedPool(_responder(finishes=["normal"], complete_returns=0))
    ih.handle_identify(pool2, FakeStorage(), {"card_id": "card-1"}, CFG)
    assert not any(e == "batch_complete" for e, _ in notified2)  # guarded: no double fire


def _patch_with_enqueue(monkeypatch, ident):
    """Like _patch, but also captures jobqueue.enqueue(...) calls."""
    notified = _patch(monkeypatch, ident)
    enqueued = []
    monkeypatch.setattr(
        ih.jobqueue, "enqueue",
        lambda pool, jtype, payload, **kw: enqueued.append((jtype, payload, kw)) or "job-id",
    )
    return notified, enqueued


def _price_jobs(enqueued):
    return [(payload, kw) for jtype, payload, kw in enqueued if jtype == "price"]


def test_resolved_card_enqueues_one_price_job_per_finish(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal", "holofoil"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)

    prices = _price_jobs(enqueued)
    payloads = sorted(p["finish"] for p, _kw in prices)
    assert payloads == ["holofoil", "normal"]                 # exactly one per finish
    assert all(p["card_ref_id"] == "sv4-1" for p, _kw in prices)
    # enqueued with batch/user context so the price jobs stay attributable
    assert all(kw.get("batch_id") == "batch-1" and kw.get("user_id") == "user-1"
               for _p, kw in prices)


def test_single_finish_card_enqueues_one_price_job(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    prices = _price_jobs(enqueued)
    assert [p["finish"] for p, _kw in prices] == ["normal"]


def test_null_card_ref_id_enqueues_no_price_jobs(monkeypatch):
    ident = _ident(None, "validation", [MethodResult("a", "sv4-2", 0.5)], ["sv4-2"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=[]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    assert _price_jobs(enqueued) == []                        # no ref id -> no price jobs


def test_nonexistent_card_id_is_a_clean_noop(monkeypatch):
    """Defensive belt-and-suspenders (Task 11 review): a card_id with no row
    (SELECT returns nothing) must not crash. identify_crop/_deps are patched to
    explode if called, proving the handler returns before ever reaching them."""
    def _boom(*a, **kw):
        raise AssertionError("must not be called for a nonexistent card_id")

    monkeypatch.setattr(ih, "identify_crop", _boom)
    monkeypatch.setattr(ih, "_deps", _boom)
    monkeypatch.setattr(ih.jobqueue, "notify_progress", _boom)

    def responder(sql, params):
        return []  # every SELECT/UPDATE returns no rows

    pool = ScriptedPool(responder)
    ih.handle_identify(pool, FakeStorage(), {"card_id": "ghost-card"}, CFG)
    # Only the initial card/photo/batch SELECT should have run.
    assert len(pool.cursor.executed) == 1
