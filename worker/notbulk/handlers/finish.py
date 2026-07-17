"""Finish-spread narrowing (design §4.4 / M2 amendment #3). INVARIANT-SENSITIVE.

Called by handle_price (handlers/price.py) after every price upsert. It may ONLY
ever clear a finish-confirmation flag and downgrade a validation-due-to-finish
card toward 'auto'. Zero wrong auto-accepts is the hard invariant, so this code
is provably conservative:

- Candidates are ONLY cards WHERE card_ref_id=? AND status='validation' AND
  finish_needs_confirmation=true AND accepted_stage IN ('h','multi','llm').
  A card in validation for identity reasons (accepted_stage='validation'), or
  already validated/corrected/skipped/merged/not_card/unreadable, is NEVER touched.
- The flag clears ONLY when EVERY finish of the card's card_refs.finishes has a
  non-NULL cached price AND the spread across those prices is <= the configured
  pct (15). A NULL known-miss, an absent price row, min==0, or spread>pct all
  leave the card untouched (it stays in validation for a human).
- On narrow: finish := first FINISH_KEYS-order key that is both in the card's
  finishes and priced; status:='auto'; finish_needs_confirmation:=false.
  card_ref_id, confidence, accepted_stage, and candidates are never touched.
- The UPDATE re-checks status='validation' AND finish_needs_confirmation=true in
  its WHERE so a concurrent human validation cannot be clobbered (the narrow is a
  no-op if the row already moved on).
"""
from __future__ import annotations

from ..pricing.sources import FINISH_KEYS

# Candidate cards: encodes the full guard at the source query. Only these rows can
# ever be narrowed.
_SELECT_CANDIDATES_SQL = (
    "SELECT id FROM cards "
    "WHERE card_ref_id=%s AND status='validation' AND finish_needs_confirmation=true "
    "AND accepted_stage IN ('h','multi','llm')"
)

_SELECT_FINISHES_SQL = "SELECT finishes FROM card_refs WHERE id = %s"

# Cached prices for exactly the card's finishes. Absent finish -> no row returned
# (=> cannot compute); NULL price_cents -> known-miss (=> cannot compute).
_SELECT_PRICES_SQL = (
    "SELECT finish, price_cents FROM prices "
    "WHERE card_ref_id=%s AND finish = ANY(%s)"
)

# Guarded narrow: the WHERE re-checks the preconditions so a concurrent validation
# cannot be clobbered (atomic re-check). Only clears the flag; never touches
# card_ref_id / confidence / accepted_stage / candidates.
_NARROW_SQL = (
    "UPDATE cards SET finish=%s, finish_needs_confirmation=false, status='auto', "
    "updated_at=now() "
    "WHERE id=%s AND status='validation' AND finish_needs_confirmation=true"
)


def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None:
    threshold = cfg["pricing"]["finish_spread_flag_pct"]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_CANDIDATES_SQL, (card_ref_id,))
            candidate_ids = [r[0] for r in cur.fetchall()]
        conn.commit()

    if not candidate_ids:
        return

    # card_refs is global reference data: finishes + prices are the same for every
    # candidate card of this card_ref_id, so read them once.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_FINISHES_SQL, (card_ref_id,))
            frow = cur.fetchone()
        conn.commit()
    finishes = list(frow[0]) if frow and frow[0] else []
    if not finishes:
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PRICES_SQL, (card_ref_id, finishes))
            price_rows = cur.fetchall()
        conn.commit()

    # Map finish -> price_cents for the cached rows we got back.
    priced: dict[str, int | None] = {f: c for (f, c) in price_rows}

    # Every finish must be present AND non-NULL, else we cannot compute -> untouched.
    if any(f not in priced or priced[f] is None for f in finishes):
        return

    values = [priced[f] for f in finishes]
    lo, hi = min(values), max(values)
    if lo <= 0:
        return  # min==0 (or negative, defensively) -> spread undefined -> untouched
    spread_pct = (hi - lo) / lo * 100.0
    if spread_pct > threshold:
        return  # finish materially affects value -> stays in validation

    # Narrow: pick the FIRST FINISH_KEYS-order key that is both a card finish and priced.
    chosen = next((k for k in FINISH_KEYS if k in finishes and priced.get(k) is not None), None)
    if chosen is None:
        return  # a card finish outside FINISH_KEYS with none of the known keys -> untouched

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for cid in candidate_ids:
                cur.execute(_NARROW_SQL, (chosen, cid))
        conn.commit()
