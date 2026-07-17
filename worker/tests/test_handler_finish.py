"""maybe_narrow_finish_flag: the finish-spread narrowing invariant guard (Task 8).

Zero wrong auto-accepts is the hard invariant. These tests cover the guard hard:
only 'validation' + finish_needs_confirmation + accepted_stage in (h,multi,llm) cards
are candidates; the flag clears ONLY when every finish is priced and the spread is
<=15%; and the narrowing UPDATE re-checks its own preconditions in the WHERE clause.

No DB: a SQL-text-matching scripted pool returns candidate rows, the card_refs
finishes, and the cached prices; assertions read the recorded UPDATE (or its absence).
"""
from __future__ import annotations

from notbulk.handlers import finish as fh


class ScriptedCursor:
    def __init__(self, responder):
        self._responder = responder
        self._current = []
        self.executed = []  # list[(sql, params)] for assertions
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


CFG = {"pricing": {"finish_spread_flag_pct": 15}}


def _responder(*, candidates, finishes, prices):
    """Build a SQL-text responder.

    candidates: rows for the candidate SELECT — each (card_id, ) tuple.
    finishes:   list[str] returned for the card_refs.finishes SELECT.
    prices:     list[(finish, price_cents)] returned for the prices SELECT
                (omit a finish entirely to model an absent row; use None cents
                to model a NULL cached known-miss).
    """
    def responder(sql, params):
        low = " ".join(sql.lower().split())
        if low.startswith("select") and "from cards" in low and "finish_needs_confirmation" in low:
            return list(candidates)
        if "select finishes from card_refs" in low:
            return [(finishes,)]
        if low.startswith("select") and "from prices" in low:
            return list(prices)
        # UPDATE cards -> pretend one row matched (fired); the guard WHERE is asserted by text.
        return [("card-1",)]
    return responder


def _update(pool):
    for sql, params in pool.cursor.executed:
        if sql.lower().startswith("update cards"):
            return sql, params
    return None, None


def test_two_finishes_small_spread_narrows_to_auto():
    # normal=1000c, holofoil=1100c -> spread 10% <= 15% -> narrow.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1100)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, params = _update(pool)
    assert sql is not None
    assert "finish_needs_confirmation=false" in sql.lower()
    assert "status='auto'" in sql.lower() or "status = 'auto'" in sql.lower()
    # finish set to FIRST FINISH_KEYS-order present+priced key = 'normal'.
    assert "normal" in params
    assert "card-1" in params


def test_large_spread_untouched():
    # normal=1000c, holofoil=1250c -> spread 25% > 15% -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1250)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None  # no narrowing UPDATE ran


def test_one_finish_null_price_untouched():
    # holofoil is a cached NULL known-miss -> cannot compute -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", None)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_one_finish_absent_from_cache_untouched():
    # holofoil has no prices row at all -> cannot compute -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_min_price_zero_untouched():
    # normal=0c -> min==0 -> spread undefined -> untouched (never divide by zero).
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 0), ("holofoil", 500)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_no_candidates_is_a_noop():
    # A card already validated by a human is not returned by the candidate SELECT
    # (status<>'validation'); the candidate query returns nothing -> no reads, no UPDATE.
    pool = ScriptedPool(_responder(
        candidates=[],           # SELECT ... status='validation' ... -> no rows
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1010)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None
    # The candidate SELECT ran; nothing else touched cards.
    assert any(s.lower().startswith("select") and "from cards" in s.lower()
               for s, _ in pool.cursor.executed)


def test_candidate_select_scopes_status_flag_and_stage():
    # The candidate SELECT itself must encode the guard: status='validation',
    # finish_needs_confirmation, accepted_stage IN ('h','multi','llm'). This proves
    # ID-uncertain (accepted_stage='validation'), already-validated, and
    # flag=false cards are excluded at the source query.
    pool = ScriptedPool(_responder(candidates=[], finishes=[], prices=[]))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sel = next(s for s, _ in pool.cursor.executed
               if s.lower().startswith("select") and "from cards" in s.lower())
    low = " ".join(sel.lower().split())
    assert "status='validation'" in low or "status = 'validation'" in low
    assert "finish_needs_confirmation" in low
    assert "accepted_stage in ('h','multi','llm')" in low or \
           "accepted_stage in ('h', 'multi', 'llm')" in low


def test_narrowing_update_where_rechecks_preconditions():
    # The guarded UPDATE re-checks status+flag in its WHERE so a concurrent human
    # validation can't be clobbered. Assert the SQL text.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1050)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    low = " ".join(sql.lower().split())
    assert "where id=" in low or "where id =" in low
    assert "status='validation'" in low or "status = 'validation'" in low
    assert "finish_needs_confirmation=true" in low or "finish_needs_confirmation = true" in low


def test_first_finish_keys_order_wins_when_narrowing():
    # Card finishes listed holofoil-first, but FINISH_KEYS precedence puts 'normal'
    # first; the chosen finish must follow FINISH_KEYS order, not the row's order.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["holofoil", "normal"],
        prices=[("holofoil", 1100), ("normal", 1000)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    _sql, params = _update(pool)
    assert "normal" in params
    assert "holofoil" not in params
