"""handle_price: cache-fresh short-circuit, resolve+upsert, NULL known-miss,
SourceUnavailable re-raise, and the post-upsert narrow call. resolve_price /
cache / finish.maybe_narrow_finish_flag are all monkeypatched — no network, no
DB, no Task-8 dependency."""
from __future__ import annotations

import pytest

from notbulk.handlers import price as ph
from notbulk.pricing.sources import SourceUnavailable

CFG = {
    "pricing": {
        "source_order": ["pokemontcg"],
        "cache_ttl_hours": 24,
        "pokemontcg_base": "https://api.pokemontcg.io/v2",
    }
}
PAYLOAD = {"card_ref_id": "sv4-123", "finish": "holofoil"}


class _Spy:
    """Captures calls so a test can assert what the handler did."""
    def __init__(self):
        self.upserts = []
        self.narrowed = []
        self.resolved = []


def _wire(monkeypatch, spy, *, fresh, cached_cents=None, resolve_result=None, resolve_raises=None):
    monkeypatch.setattr(ph.cache, "read_cached",
                        lambda pool, cr, fin, ttl: (fresh, cached_cents))

    def _resolve(sources, cr, fin, cfg):
        spy.resolved.append((cr, fin))
        if resolve_raises is not None:
            raise resolve_raises
        return resolve_result

    monkeypatch.setattr(ph, "resolve_price", _resolve)
    monkeypatch.setattr(ph.cache, "upsert_price",
                        lambda pool, cr, fin, cents, source: spy.upserts.append((cr, fin, cents, source)))
    monkeypatch.setattr(ph.finish, "maybe_narrow_finish_flag",
                        lambda pool, cr, cfg: spy.narrowed.append(cr))


def test_fresh_cache_is_a_noop():
    spy = _Spy()
    import pytest as _pt
    with _pt.MonkeyPatch.context() as mp:
        _wire(mp, spy, fresh=True, cached_cents=1234)
        ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.resolved == []      # resolve NOT called on a fresh hit
    assert spy.upserts == []       # nothing re-written
    assert spy.narrowed == []      # no narrow on a no-op


def test_resolves_and_upserts_then_narrows(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(1234, "pokemontcg"))
    ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.resolved == [("sv4-123", "holofoil")]
    assert spy.upserts == [("sv4-123", "holofoil", 1234, "pokemontcg")]
    assert spy.narrowed == ["sv4-123"]   # narrow invoked AFTER a successful upsert


def test_genuine_miss_upserts_null(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(None, "pokemontcg"))
    ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.upserts == [("sv4-123", "holofoil", None, "pokemontcg")]   # NULL known-miss cached
    assert spy.narrowed == ["sv4-123"]   # a cached miss still triggers narrow evaluation


def test_source_unavailable_reraises_and_does_not_upsert(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_raises=SourceUnavailable("down"))
    with pytest.raises(SourceUnavailable):
        ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.upserts == []        # no cache write on transient failure
    assert spy.narrowed == []       # and no narrow


def test_missing_payload_key_raises_value_error(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(1234, "pokemontcg"))
    with pytest.raises(ValueError):
        ph.handle_price(pool=object(), storage=object(), payload={"card_ref_id": "sv4-123"}, cfg=CFG)


def test_stub_price_seam_upserts_canned_without_resolving(monkeypatch):
    """NOTBULK_STUB_PRICE=1: handle_price upserts (1234,'pokemontcg') for the
    payload finish WITHOUT calling resolve_price (the network path). Inert-when-
    unset behavior is covered by the non-stub tests in this file."""
    monkeypatch.setenv("NOTBULK_STUB_PRICE", "1")

    def _boom(*a, **kw):
        raise AssertionError("resolve_price must NOT be called under NOTBULK_STUB_PRICE")

    monkeypatch.setattr(ph, "resolve_price", _boom)
    # No-op the downstream narrow so this test isolates the price upsert.
    monkeypatch.setattr(ph.finish, "maybe_narrow_finish_flag", lambda pool, cid, cfg: None)

    upserts = []
    monkeypatch.setattr(
        ph.cache, "upsert_price",
        lambda pool, cid, fin, cents, src: upserts.append((cid, fin, cents, src)),
    )
    # read_cached returns (fresh=False, None) so the resolve/upsert path runs.
    monkeypatch.setattr(ph.cache, "read_cached", lambda pool, cid, fin, ttl: (False, None))

    ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)

    assert upserts == [("sv4-123", "holofoil", 1234, "pokemontcg")]
