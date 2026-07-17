"""Worker-loop wiring tests: a terminal job failure emits a sanitized Discord
error notify carrying the exception CLASS name (never str(exc) / a traceback).

No network, no real DB: FakePool feeds the claim + fail SQL, notify is a spy.
"""
from __future__ import annotations

from notbulk import worker
from tests.fakes import FakePool


TEST_CFG = {"discord": {"enabled": False, "timeout_seconds": 5}}


def _boom_handler(pool, storage, payload, cfg):
    raise ValueError("secret filename /home/u/IMG_4211.heic leaked into message")


def test_terminal_failure_emits_error_notify_with_class_only(monkeypatch):
    # claim() -> one 'detect' job; fail() -> RETURNING 'failed' (terminal).
    pool = FakePool([
        [("job-1", "detect", {"photo_id": "p1"})],   # _CLAIM_SQL RETURNING id,type,payload
        [("failed",)],                                # _FAIL_DEAD_SQL RETURNING status
    ])
    spy: list[tuple] = []
    monkeypatch.setattr(worker.discord, "notify",
                        lambda cfg, level, title, fields: spy.append((level, title, fields)))

    handled = worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": _boom_handler}, worker_id="w1",
    )
    assert handled is True
    assert len(spy) == 1
    level, title, fields = spy[0]
    assert level == "error"
    assert title == "pipeline job failed"
    assert fields["type"] == "detect"
    assert fields["job_id"] == "job-1"
    assert fields["error_class"] == "ValueError"     # CLASS name only
    # The raw message (with the leaked filename) is NEVER in the notify fields.
    assert all("IMG_4211" not in str(v) for v in fields.values())


def test_success_emits_no_notify(monkeypatch):
    pool = FakePool([
        [("job-2", "detect", {"photo_id": "p1"})],   # claim
        [],                                           # complete (no RETURNING)
    ])
    spy: list = []
    monkeypatch.setattr(worker.discord, "notify",
                        lambda *a, **k: spy.append(a))
    worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": lambda *a: None}, worker_id="w1",
    )
    assert spy == []
