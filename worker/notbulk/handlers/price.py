"""price job handler.

payload {card_ref_id, finish}:
  read_cached -> if fresh (incl. a fresh NULL known-miss), no-op and return.
  else: build the configured source list (cfg.pricing.source_order), resolve_price,
        upsert (a genuine miss stores price_cents=NULL so we don't re-hit within TTL).
        SourceUnavailable from resolve_price PROPAGATES so the job retries via the
        queue's attempts/backoff (worker.py's fail(dead=False)).
  after a successful upsert, invoke finish.maybe_narrow_finish_flag (Task 8) so a
  now-priced finish can clear a finish_needs_confirmation flag when the spread is small.

Money is integer cents throughout (plan Global Constraints).
"""
from __future__ import annotations

import os

from .. import jobqueue
from ..pricing import cache
from ..pricing.sources import (CollectrPriceSource, PokemonTcgPriceSource,
                               resolve_price)
from . import finish

# name -> PriceSource class. cfg.pricing.source_order lists the names to try, in order.
_SOURCE_REGISTRY = {
    "pokemontcg": PokemonTcgPriceSource,
    "collectr": CollectrPriceSource,
}


def _build_sources(cfg: dict) -> list:
    order = cfg["pricing"]["source_order"]
    return [_SOURCE_REGISTRY[name]() for name in order]


def handle_price(pool, storage, payload: dict, cfg: dict) -> None:
    # Payload shape is enforced by jobqueue.validate_payload at dispatch, but this
    # keeps the handler safe if called directly (tests) — same defensive read as peers.
    jobqueue.validate_payload("price", payload)
    card_ref_id = payload["card_ref_id"]
    finish_key = payload["finish"]
    ttl_hours = int(cfg["pricing"]["cache_ttl_hours"])

    fresh, _cents = cache.read_cached(pool, card_ref_id, finish_key, ttl_hours)
    if fresh:
        # A fresh row (real price OR a fresh NULL known-miss) needs no refetch.
        return

    # Test-only seam: deterministic offline pricing for the M3 E2E loop.
    # When NOTBULK_STUB_PRICE is set, skip the network resolve and use a canned
    # price so the queue/cache/narrow/explorer path can be exercised without
    # hitting pokemontcg.io. Never set in production. (Mirrors NOTBULK_STUB_IDENTIFY.)
    if os.environ.get("NOTBULK_STUB_PRICE") == "1":
        price_cents, source_name = 1234, "pokemontcg"
    else:
        # resolve_price raises SourceUnavailable if every source skips — let it
        # propagate so the queue retries the job (do NOT cache a transient failure).
        price_cents, source_name = resolve_price(
            _build_sources(cfg), card_ref_id, finish_key, cfg
        )
    # A genuine miss (price_cents is None) is cached as a known-miss so we don't
    # re-hit the API within the TTL.
    cache.upsert_price(pool, card_ref_id, finish_key, price_cents, source_name)

    # Now that this finish is priced, a card deferred purely on finish spread may
    # be narrowable (Task 8's strict guard decides; never a wrong auto-accept).
    finish.maybe_narrow_finish_flag(pool, card_ref_id, cfg)
