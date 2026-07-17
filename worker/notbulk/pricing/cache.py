"""The prices cache: read_cached (with TTL freshness) and upsert_price.

A NULL price_cents row is a CACHED KNOWN-MISS — real "no data", not $0, and it
counts as fresh within the TTL so we don't re-hit the API for a card that simply
has no market price (plan Global Constraints / §5). Freshness is evaluated in the
query so a NULL price row is still 'fresh'.
"""
from __future__ import annotations

# Freshness filter in-DB: a row is fresh iff fetched_at is within ttl_hours of now.
# make_interval(hours => %s) keeps ttl_hours a bound parameter (never string-formatted).
_READ_SQL = (
    "SELECT price_cents, fetched_at FROM prices "
    "WHERE card_ref_id = %s AND finish = %s "
    "AND fetched_at > now() - make_interval(hours => %s)"
)

_UPSERT_SQL = (
    "INSERT INTO prices (card_ref_id, finish, price_cents, source, fetched_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (card_ref_id, finish) DO UPDATE SET "
    "price_cents = EXCLUDED.price_cents, source = EXCLUDED.source, fetched_at = now()"
)


def read_cached(pool, card_ref_id: str, finish: str, ttl_hours: int) -> tuple[bool, int | None]:
    """Return (fresh, price_cents). fresh=True iff a row exists with fetched_at
    within ttl_hours (a NULL price_cents row is a valid fresh known-miss). A stale
    or absent row -> (False, None)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_READ_SQL, (card_ref_id, finish, ttl_hours))
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return False, None
    price_cents = row[0]   # may be None (known-miss); still a fresh hit
    return True, price_cents


def upsert_price(pool, card_ref_id: str, finish: str, price_cents: int | None, source: str) -> None:
    """Insert or refresh the cache row for (card_ref_id, finish). price_cents=None
    stores a known-miss; fetched_at is set to now() on both insert and update."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (card_ref_id, finish, price_cents, source))
        conn.commit()
