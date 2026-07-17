"""read_cached / upsert_price against FakePool. No real DB. Asserts both the
(fresh, cents) tuple and the SQL params bound."""
from __future__ import annotations

from notbulk.pricing import cache
from tests.fakes import FakePool


def test_read_cached_fresh_known_price():
    # A fresh row with a real price: the SELECT returns one row (price_cents, fetched_at).
    pool = FakePool([[(1234, "2026-07-16T00:00:00Z")]])
    fresh, cents = cache.read_cached(pool, "sv4-123", "holofoil", 24)
    assert (fresh, cents) == (True, 1234)
    sql, params = pool.cursor.executed[0]
    assert "from prices" in sql.lower()
    assert "make_interval" in sql.lower()
    # params: (card_ref_id, finish, ttl_hours) — order per the query
    assert params == ("sv4-123", "holofoil", 24)


def test_read_cached_fresh_null_known_miss():
    # A fresh row whose price_cents is NULL is a VALID known-miss: fresh=True, cents=None.
    pool = FakePool([[(None, "2026-07-16T00:00:00Z")]])
    fresh, cents = cache.read_cached(pool, "sv4-5", "holofoil", 24)
    assert (fresh, cents) == (True, None)


def test_read_cached_absent_or_stale_returns_not_fresh():
    # No row within TTL (absent OR older than TTL — the SQL filters stale out): empty result.
    pool = FakePool([[]])
    fresh, cents = cache.read_cached(pool, "sv4-999", "normal", 24)
    assert (fresh, cents) == (False, None)


def test_upsert_price_binds_all_columns():
    pool = FakePool([[]])   # upsert returns nothing
    cache.upsert_price(pool, "sv4-123", "holofoil", 1234, "pokemontcg")
    sql, params = pool.cursor.executed[0]
    low = sql.lower()
    assert "insert into prices" in low
    assert "on conflict (card_ref_id, finish) do update" in low
    assert params == ("sv4-123", "holofoil", 1234, "pokemontcg")


def test_upsert_price_stores_null_known_miss():
    pool = FakePool([[]])
    cache.upsert_price(pool, "sv4-5", "holofoil", None, "pokemontcg")
    _sql, params = pool.cursor.executed[0]
    assert params == ("sv4-5", "holofoil", None, "pokemontcg")   # NULL cents preserved
