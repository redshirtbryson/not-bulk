"""handle_fetch: photo (status 'fetching') -> direct fetch or album/gallery
enumeration + fanout -> photo stored/failed -> notify. All network (fetch_image
/ enumerate_imgur / enumerate_reddit) and the byte gate (gate_bytes) and
storage are faked; no real I/O, no real Postgres.

Uses the same ScriptedCursor/ScriptedConn/ScriptedPool pattern as
test_handler_detect.py: the handler runs several distinct queries per call, so
rows are scripted by matching on the SQL text rather than a flat
pop-in-order list.
"""
from __future__ import annotations

import pytest

from notbulk.fetcher import FetchRejected
from notbulk.handlers import fetch as fetch_handler
from notbulk.imagegate import GateRejected


# ---- test doubles ---------------------------------------------------------

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
    def __init__(self):
        self.puts = []            # (key, body, content_type)

    def put(self, key, body, content_type):
        self.puts.append((key, body, content_type))

    @staticmethod
    def photo_key(u, b, p):
        return f"{u}/{b}/{p}.webp"


CFG = {
    "quotas": {
        "photos_per_day": 50,
        "fetches_per_day": 20,
        "photos_per_batch": 10,
        "max_photo_bytes": 10485760,
        "max_pixels": 50000000,
    },
    "fetcher": {
        "allowed_hosts": ["i.imgur.com", "imgur.com", "api.imgur.com",
                          "i.redd.it", "www.reddit.com"],
        "max_bytes": 15728640,
        "timeout_seconds": 20,
    },
}


def _make_responder(source_url="https://i.imgur.com/direct.png",
                    batch_photo_count=1, usage_photos=0, usage_fetches=0):
    """Photo row: (id, batch_id, source_url, user_id). Batch photo count and
    today's usage row are also scripted so _fanout's peek queries resolve."""

    def responder(sql, params):
        low = " ".join(sql.lower().split())
        if "from photos p join batches b" in low:
            return [("photo-1", "batch-1", source_url, "user-1")]
        if "count(*) from photos" in low:
            return [(batch_photo_count,)]
        if "coalesce(photos, 0), coalesce(fetches, 0) from usage" in low:
            return [(usage_photos, usage_fetches)]
        if "insert into usage" in low:
            # all-or-nothing reservation: RETURNING user_id on success.
            # Caller pre-clamps `want` from the peek, so this always succeeds
            # in these tests (no simulated race).
            return [("user-1",)]
        return []
    return responder


# ---- direct-image path -----------------------------------------------------

def test_handle_fetch_direct_image_stores_and_enqueues(monkeypatch):
    monkeypatch.setattr(fetch_handler, "fetch_image", lambda url, cfg: b"rawbytes")
    monkeypatch.setattr(fetch_handler, "gate_bytes",
                        lambda raw, cfg: (b"webpbytes", 100, 100))

    enqueued = []
    notified = []
    monkeypatch.setattr(fetch_handler.jobqueue, "enqueue",
                        lambda pool, jt, payload, **kw: enqueued.append((jt, payload)) or "job")
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress",
                        lambda pool, batch_id, event, **ids: notified.append((event, ids)))

    pool = ScriptedPool(_make_responder(source_url="https://i.imgur.com/direct.png"))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    assert len(storage.puts) == 1
    key, body, ctype = storage.puts[0]
    assert body == b"webpbytes"
    assert ctype == "image/webp"
    assert [jt for jt, _ in enqueued] == ["detect"]
    assert enqueued[0][1] == {"photo_id": "photo-1"}
    assert ("photo_stored", {"photo_id": "photo-1"}) in notified

    inc_storage = [s for s, _ in pool.cursor.executed if "storage_bytes_used" in s.lower()]
    assert len(inc_storage) == 1
    mark_stored = [s for s, _ in pool.cursor.executed if "status='stored'" in s.lower()]
    assert len(mark_stored) == 1

    # No fanout activity for a direct URL: no additional photo INSERTs.
    photo_inserts = [s for s, _ in pool.cursor.executed if "insert into photos" in s.lower()]
    assert photo_inserts == []


# ---- album/gallery path -----------------------------------------------------

def test_handle_fetch_album_fanout_truncated_by_daily_headroom(monkeypatch):
    """Album offers 5 images. photos_per_batch has plenty of room (cap 10,
    batch already has 1 photo -> room 9), but daily headroom is tight: only 2
    MORE photos/fetches are allowed today (usage already at photos=48,
    fetches=18 against caps 50/20 -> photos_room=2, fetches_room=2).

    First image goes into THIS photo row (no quota consumed by _fanout for
    it -- that was reserved at batch-create). The remaining 4 offered by the
    album must be truncated to 2 by daily headroom, NOT by photos_per_batch.
    Usage must be incremented by exactly the 2 rows actually created.

    This assertion is a regression guard for FIX 1: against the pre-fix
    _fanout (which only checked `cap - current` against photos_per_batch and
    never touched usage.photos/usage.fetches at all), room would be
    min(9, len(remaining)=4) = 4 -- all 4 extra photo rows would be created,
    blowing through the daily headroom of 2. That is exactly the quota-bypass
    bug FIX 1 closes; this test fails against the pre-fix code.
    """
    urls = [f"https://i.imgur.com/img{i}.png" for i in range(5)]
    monkeypatch.setattr(fetch_handler, "enumerate_imgur", lambda url, cfg: urls)
    monkeypatch.setattr(fetch_handler, "fetch_image", lambda url, cfg: b"rawbytes")
    monkeypatch.setattr(fetch_handler, "gate_bytes",
                        lambda raw, cfg: (b"webpbytes", 100, 100))

    enqueued = []
    notified = []
    monkeypatch.setattr(fetch_handler.jobqueue, "enqueue",
                        lambda pool, jt, payload, **kw: enqueued.append((jt, payload)) or "job")
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress",
                        lambda pool, batch_id, event, **ids: notified.append((event, ids)))

    pool = ScriptedPool(_make_responder(
        source_url="https://imgur.com/a/abc123",
        batch_photo_count=1,       # room under photos_per_batch(10) = 9
        usage_photos=48,           # photos_room = 50 - 48 = 2
        usage_fetches=18,          # fetches_room = 20 - 18 = 2
    ))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    # First url stored directly into photo-1.
    assert len(storage.puts) == 1

    # Fanout created exactly 2 additional photo rows (truncated by daily
    # headroom, not by the looser photos_per_batch room of 9).
    photo_inserts = [p for s, p in pool.cursor.executed if "insert into photos" in s.lower()]
    assert len(photo_inserts) == 2

    fanout_enqueues = [p for jt, p in enqueued if jt == "fetch_source"]
    assert len(fanout_enqueues) == 2

    # Usage was incremented by exactly the created count (2), not the offered
    # remainder (4) and not the photos_per_batch room (9).
    usage_inserts = [(s, p) for s, p in pool.cursor.executed if "insert into usage" in s.lower()]
    assert len(usage_inserts) == 1
    _sql, params = usage_inserts[0]
    assert params["want"] == 2


def test_handle_fetch_album_fanout_truncated_by_photos_per_batch(monkeypatch):
    """Plenty of daily headroom, but the batch is nearly full against
    photos_per_batch (cap 10, batch already has 9 -> room 1). Album offers 3
    extra images beyond the first; fanout must create only 1.
    """
    urls = [f"https://i.imgur.com/img{i}.png" for i in range(4)]
    monkeypatch.setattr(fetch_handler, "enumerate_imgur", lambda url, cfg: urls)
    monkeypatch.setattr(fetch_handler, "fetch_image", lambda url, cfg: b"rawbytes")
    monkeypatch.setattr(fetch_handler, "gate_bytes",
                        lambda raw, cfg: (b"webpbytes", 100, 100))

    enqueued = []
    monkeypatch.setattr(fetch_handler.jobqueue, "enqueue",
                        lambda pool, jt, payload, **kw: enqueued.append((jt, payload)) or "job")
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress", lambda *a, **k: None)

    pool = ScriptedPool(_make_responder(
        source_url="https://imgur.com/a/abc123",
        batch_photo_count=9,      # room under photos_per_batch(10) = 1
        usage_photos=0,
        usage_fetches=0,
    ))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    photo_inserts = [p for s, p in pool.cursor.executed if "insert into photos" in s.lower()]
    assert len(photo_inserts) == 1
    fanout_enqueues = [p for jt, p in enqueued if jt == "fetch_source"]
    assert len(fanout_enqueues) == 1


def test_handle_fetch_album_no_extra_images_skips_fanout_reservation(monkeypatch):
    """A single-image album: nothing left to fan out. _fanout must not touch
    usage at all (no reservation query issued)."""
    urls = ["https://i.imgur.com/only.png"]
    monkeypatch.setattr(fetch_handler, "enumerate_imgur", lambda url, cfg: urls)
    monkeypatch.setattr(fetch_handler, "fetch_image", lambda url, cfg: b"rawbytes")
    monkeypatch.setattr(fetch_handler, "gate_bytes",
                        lambda raw, cfg: (b"webpbytes", 100, 100))
    monkeypatch.setattr(fetch_handler.jobqueue, "enqueue", lambda *a, **k: "job")
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress", lambda *a, **k: None)

    pool = ScriptedPool(_make_responder(source_url="https://imgur.com/a/abc123"))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    usage_touches = [s for s, _ in pool.cursor.executed
                     if "usage" in s.lower()]
    assert usage_touches == []


# ---- rejection paths --------------------------------------------------------

def test_handle_fetch_fetch_rejected_marks_photo_failed(monkeypatch):
    def raise_rejected(url, cfg):
        raise FetchRejected("host not on allowlist")
    monkeypatch.setattr(fetch_handler, "fetch_image", raise_rejected)

    notified = []
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress",
                        lambda pool, batch_id, event, **ids: notified.append((event, ids)))
    monkeypatch.setattr(fetch_handler.jobqueue, "enqueue", lambda *a, **k: "job")

    pool = ScriptedPool(_make_responder(source_url="https://i.imgur.com/direct.png"))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    mark_failed = [s for s, p in pool.cursor.executed if "status='failed'" in s.lower()]
    assert len(mark_failed) == 1
    assert ("photo_done", {"photo_id": "photo-1"}) in notified
    assert storage.puts == []


def test_handle_fetch_gate_rejected_marks_photo_failed(monkeypatch):
    monkeypatch.setattr(fetch_handler, "fetch_image", lambda url, cfg: b"notanimage")

    def raise_gate_rejected(raw, cfg):
        raise GateRejected("bad magic bytes")
    monkeypatch.setattr(fetch_handler, "gate_bytes", raise_gate_rejected)

    notified = []
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress",
                        lambda pool, batch_id, event, **ids: notified.append((event, ids)))

    pool = ScriptedPool(_make_responder(source_url="https://i.imgur.com/direct.png"))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    mark_failed = [s for s, p in pool.cursor.executed if "status='failed'" in s.lower()]
    assert len(mark_failed) == 1
    assert ("photo_done", {"photo_id": "photo-1"}) in notified
    assert storage.puts == []


def test_handle_fetch_empty_album_rejected_marks_photo_failed(monkeypatch):
    monkeypatch.setattr(fetch_handler, "enumerate_imgur", lambda url, cfg: [])

    notified = []
    monkeypatch.setattr(fetch_handler.jobqueue, "notify_progress",
                        lambda pool, batch_id, event, **ids: notified.append((event, ids)))

    pool = ScriptedPool(_make_responder(source_url="https://imgur.com/a/abc123"))
    storage = FakeStorage()

    fetch_handler.handle_fetch(pool, storage, {"photo_id": "photo-1"}, CFG)

    mark_failed = [s for s, p in pool.cursor.executed if "status='failed'" in s.lower()]
    assert len(mark_failed) == 1
    assert ("photo_done", {"photo_id": "photo-1"}) in notified
