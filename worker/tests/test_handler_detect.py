"""handle_detect: photo -> detect_cards -> crop store -> card rows -> identify
jobs -> photo done -> notify. All DB/storage/detection are faked; no cv2 model,
no real Postgres.

FakePool here is a richer stand-in than tests/fakes.FakePool: the handler runs
several distinct queries in one connection, so we script rows by a matcher on
the SQL text rather than a flat pop-in-order list.

The card INSERT uses `... ON CONFLICT (photo_id, crop_index) DO NOTHING
RETURNING id`, so the fake must model conflicts HONESTLY: a genuinely-new
crop_index returns a one-row result (the new id); a crop_index that already has
a card returns ZERO rows. The handler branches on that to skip re-store/enqueue
on retry — so the fake tracks a set of pre-existing crop_indexes per photo.
"""
from __future__ import annotations

import numpy as np
import pytest

from notbulk.handlers import detect as detect_handler
from notbulk.types import Detection


# ---- test doubles --------------------------------------------------------

class ScriptedCursor:
    def __init__(self, responder):
        self._responder = responder      # (sql, params) -> list[row]
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
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

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
    def __init__(self, photo_bytes: bytes):
        self._photo_bytes = photo_bytes
        self.puts = []            # (key, body, content_type)

    def get(self, key):
        return self._photo_bytes

    def put(self, key, body, content_type):
        self.puts.append((key, body, content_type))

    @staticmethod
    def photo_key(u, b, p):
        return f"{u}/{b}/{p}.webp"

    @staticmethod
    def crop_key(u, b, c):
        return f"{u}/{b}/crops/{c}.webp"


CFG = {
    "crop": {"width": 734, "height": 1024, "webp_quality": 80},
    "detection": {"aspect": 0.714, "aspect_tolerance": 0.12, "min_area_frac": 0.005,
                  "max_cards_per_photo": 30, "sharpness_min": 45.0},
    "quotas": {"cards_per_day": 600},
}


def _canned_detection(idx):
    crop = np.full((1024, 734, 3), 100 + idx * 20, dtype=np.uint8)
    quad = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
    return Detection(quad=quad, crop=crop, sharpness=99.0, crop_index=idx)


def _make_responder(photo_status="stored", allowance=99, existing_crop_indexes=()):
    """Return a responder simulating the staged DB rows for one photo.

    Photo row: (id, batch_id, status, user_id). Quota upsert returns the
    granted allowance. The card INSERT ... RETURNING id returns one row when the
    crop_index is NEW and zero rows when it is in ``existing_crop_indexes``
    (models ON CONFLICT DO NOTHING RETURNING). crop_index is the 3rd INSERT
    param. Status updates / crop_key updates / notify return nothing.
    """
    existing = set(existing_crop_indexes)

    def responder(sql, params):
        low = " ".join(sql.lower().split())
        if "from photos" in low and "select" in low:
            # photo row: status + batch + user
            return [("photo-1", "batch-1", photo_status, "user-1")]
        if "count(*) from cards" in low:
            # retry detection: number of cards already present for this photo.
            return [(len(existing),)]
        if "insert into usage" in low or "update usage" in low:
            # cards-per-day conditional upsert -> granted count
            return [(allowance,)]
        if "insert into cards" in low:
            # params: (card_id, photo_id, crop_index) — RETURNING id yields a
            # row only when this crop_index is genuinely new.
            crop_index = params[2]
            if crop_index in existing:
                return []               # conflict: no row returned
            return [(params[0],)]       # inserted: returns the new card id
        return []
    return responder


def test_handle_detect_inserts_cards_stores_crops_enqueues_identify(monkeypatch):
    dets = [_canned_detection(0), _canned_detection(1)]
    monkeypatch.setattr(detect_handler, "detect_cards", lambda photo, cfg: dets)

    enqueued = []
    monkeypatch.setattr(
        detect_handler.jobqueue, "enqueue",
        lambda pool, jt, payload, **kw: enqueued.append((jt, payload)) or "job-x",
    )
    notified = []
    monkeypatch.setattr(
        detect_handler.jobqueue, "notify_progress",
        lambda pool, batch_id, event, **ids: notified.append((event, ids)),
    )

    pool = ScriptedPool(_make_responder(photo_status="stored", allowance=99))
    storage = FakeStorage(photo_bytes=b"\x89PNGfakephoto")
    # decode is faked so we don't need a real encoded image
    monkeypatch.setattr(detect_handler.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1200, 1600, 3), np.uint8))
    monkeypatch.setattr(detect_handler.cv2, "imencode",
                        lambda ext, img, params=None: (True, np.frombuffer(b"webp", np.uint8)))

    detect_handler.handle_detect(pool, storage, {"photo_id": "photo-1"}, CFG)

    # two identify jobs enqueued, one per card
    assert [jt for jt, _ in enqueued] == ["identify", "identify"]
    # two crop WebP puts
    assert len(storage.puts) == 2
    assert all(ct == "image/webp" for _k, _b, ct in storage.puts)
    # photo marked done + notify photo_done
    assert ("photo_done", {"photo_id": "photo-1"}) in notified
    inserts = [s for s, _ in pool.cursor.executed if "insert into cards" in s.lower()]
    assert len(inserts) == 2


def test_handle_detect_idempotent_when_photo_already_done(monkeypatch):
    called = {"detect": False}
    monkeypatch.setattr(detect_handler, "detect_cards",
                        lambda p, c: called.__setitem__("detect", True) or [])
    pool = ScriptedPool(_make_responder(photo_status="done"))
    storage = FakeStorage(photo_bytes=b"x")

    detect_handler.handle_detect(pool, storage, {"photo_id": "photo-1"}, CFG)

    assert called["detect"] is False   # no-op: detect_cards never runs


def test_handle_detect_truncates_to_quota_allowance(monkeypatch):
    dets = [_canned_detection(0), _canned_detection(1), _canned_detection(2)]
    monkeypatch.setattr(detect_handler, "detect_cards", lambda p, c: dets)
    monkeypatch.setattr(detect_handler.jobqueue, "enqueue", lambda *a, **k: "job")
    monkeypatch.setattr(detect_handler.jobqueue, "notify_progress", lambda *a, **k: None)
    monkeypatch.setattr(detect_handler.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1200, 1600, 3), np.uint8))
    monkeypatch.setattr(detect_handler.cv2, "imencode",
                        lambda ext, img, params=None: (True, np.frombuffer(b"webp", np.uint8)))

    logs = []
    monkeypatch.setattr(detect_handler, "_log_truncation",
                        lambda *a: logs.append(a))

    pool = ScriptedPool(_make_responder(allowance=1))   # only 1 card allowed
    storage = FakeStorage(photo_bytes=b"x")
    detect_handler.handle_detect(pool, storage, {"photo_id": "photo-1"}, CFG)

    inserts = [s for s, _ in pool.cursor.executed if "insert into cards" in s.lower()]
    assert len(inserts) == 1          # truncated to allowance
    assert logs                        # truncation logged


def test_handle_detect_retry_skips_already_inserted_crop(monkeypatch):
    """Mid-batch crash/retry: photo is still 'stored' and crop_index 0 already
    has a card row (its identify was enqueued on the first pass). On re-run the
    INSERT for crop_index 0 conflicts (no row returned) so it must NOT re-store
    or re-enqueue — no orphaned crop blob, no phantom identify job. crop_index 1
    is genuinely new and DOES get stored + enqueued.
    """
    dets = [_canned_detection(0), _canned_detection(1)]
    monkeypatch.setattr(detect_handler, "detect_cards", lambda p, c: dets)

    enqueued = []
    monkeypatch.setattr(
        detect_handler.jobqueue, "enqueue",
        lambda pool, jt, payload, **kw: enqueued.append((jt, payload)) or "job",
    )
    monkeypatch.setattr(detect_handler.jobqueue, "notify_progress", lambda *a, **k: None)
    monkeypatch.setattr(detect_handler.cv2, "imdecode",
                        lambda buf, flags: np.zeros((1200, 1600, 3), np.uint8))
    monkeypatch.setattr(detect_handler.cv2, "imencode",
                        lambda ext, img, params=None: (True, np.frombuffer(b"webp", np.uint8)))

    # crop_index 0 already exists as a card row from the interrupted first pass.
    pool = ScriptedPool(
        _make_responder(photo_status="stored", allowance=99,
                        existing_crop_indexes=(0,))
    )
    storage = FakeStorage(photo_bytes=b"x")
    detect_handler.handle_detect(pool, storage, {"photo_id": "photo-1"}, CFG)

    # exactly one store + one enqueue, both for the genuinely-new crop_index 1.
    assert len(storage.puts) == 1
    assert len(enqueued) == 1
    assert enqueued[0][0] == "identify"
