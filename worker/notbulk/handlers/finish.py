"""finish-spread narrowing (invoked by handle_price after a finish is priced).

STUB placeholder created in Task 3 so handle_price can import it; the real strict
guarded implementation lands in Task 8. Until then this is a safe no-op — a
no-op here can never cause a wrong auto-accept (it simply leaves the card in
validation), preserving the hard invariant.
"""
from __future__ import annotations


def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None:
    """No-op stub (Task 8 replaces this with the guarded narrowing logic)."""
    return None
