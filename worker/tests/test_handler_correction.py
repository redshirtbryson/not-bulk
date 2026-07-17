"""handle_ingest_correction: crop -> INSERT corrections row (crop_hash sha256 of
raw webp bytes, predicted_ref_id passthrough, actual_ref_id) -> compute_hashes ->
5 ref_hashes INSERTs (source 'user_validated', signed-bigint) -> cap eviction of
oldest rows beyond the per-card cap."""
from __future__ import annotations

import numpy as np
import pytest

from notbulk.handlers import correction as ch


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

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)

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
        return b"cropbytes"

    @staticmethod
    def crop_key(u, b, c):
        return f"{u}/{b}/crops/{c}.webp"


CFG = {"crop": {"width": 734, "height": 1024, "webp_quality": 80},
       "hash": {"user_validated_cap_per_card": 20}}


def _responder(sql, params):
    low = " ".join(sql.lower().split())
    if "from cards" in low and "select" in low:
        return [("card-1", "batch-1", "user-1", "user-1/batch-1/crops/card-1.webp")]
    return []


def test_correction_writes_corrections_row_five_hashes_and_evicts(monkeypatch):
    monkeypatch.setattr(ch.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1024, 734, 3), np.uint8))
    pool = ScriptedPool(_responder)
    ch.handle_ingest_correction(
        pool, FakeStorage(),
        {"card_id": "card-1", "actual_ref_id": "sv4-9", "predicted_ref_id": "sv4-8"},
        CFG,
    )

    # Corrections row: written exactly once, BEFORE the ref_hashes inserts.
    corrections = [(s, p) for s, p in pool.cursor.executed
                   if "insert into corrections" in s.lower()]
    assert len(corrections) == 1
    _cs, cp = corrections[0]
    # params: (id, card_id, crop_hash, predicted_ref_id, actual_ref_id)
    assert cp[1] == "card-1"                                  # card_id
    assert isinstance(cp[2], str) and len(cp[2]) == 64        # crop_hash = sha256 hex
    assert all(ch_ in "0123456789abcdef" for ch_ in cp[2])    # lowercase hex
    assert cp[3] == "sv4-8"                                   # predicted_ref_id passthrough
    assert cp[4] == "sv4-9"                                   # actual_ref_id

    inserts = [(s, p) for s, p in pool.cursor.executed
               if "insert into ref_hashes" in s.lower()]
    assert len(inserts) == 5                                  # 5 hash types
    for _s, p in inserts:
        assert "user_validated" in p                          # source column
        assert "sv4-9" in p                                   # actual_ref_id
        assert -(2 ** 63) <= [x for x in p if isinstance(x, int)][0] <= 2 ** 63 - 1
    evicts = [s for s, _ in pool.cursor.executed
              if "delete from ref_hashes" in s.lower()]
    assert evicts                                             # cap-eviction runs
    low = " ".join(evicts[0].lower().split())
    assert "row_number() over" in low                         # window-function eviction
    assert "user_validated" in low


def test_correction_passes_null_predicted_ref_id(monkeypatch):
    """predicted_ref_id may be None (card had no prior prediction) — passed through."""
    monkeypatch.setattr(ch.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1024, 734, 3), np.uint8))
    pool = ScriptedPool(_responder)
    ch.handle_ingest_correction(
        pool, FakeStorage(),
        {"card_id": "card-1", "actual_ref_id": "sv4-9", "predicted_ref_id": None},
        CFG,
    )
    corrections = [(s, p) for s, p in pool.cursor.executed
                   if "insert into corrections" in s.lower()]
    assert len(corrections) == 1
    assert corrections[0][1][3] is None                       # predicted_ref_id = None
    assert len(corrections[0][1][2]) == 64                    # crop_hash still computed
