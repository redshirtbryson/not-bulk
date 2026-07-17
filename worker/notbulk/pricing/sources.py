"""Pluggable price sources.

A PriceSource maps (card_ref_id, finish) -> price in integer CENTS, or None for a
genuine miss (the card/finish simply has no market price). Transient failures
(429/5xx/transport) raise SourceUnavailable so the caller can try the next source
or, ultimately, let the price job retry via the queue's attempts/backoff.

Money is integer cents end to end (design §... / plan Global Constraints): a USD
float market price is rounded to the nearest cent exactly once, here.
"""
from __future__ import annotations

import os
import time
from typing import Protocol, runtime_checkable

import httpx

# tcgplayer price keys, verbatim — matches card_refs.finishes ordering used by the
# finish-spread rule (Task 8). Kept as the canonical finish vocabulary for M3.
FINISH_KEYS: tuple[str, ...] = ("normal", "holofoil", "reverseHolofoil")

_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BACKOFF_BASE = 1.0


class SourceUnavailable(Exception):
    """Transient source failure — try the next source, or retry the job later."""


class SourceNotConfigured(SourceUnavailable):
    """The source has no credentials/access yet (a permanent-for-now skip that is
    still treated as 'unavailable' so resolve_price moves on)."""


@runtime_checkable
class PriceSource(Protocol):
    name: str

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        """Return price in CENTS, or None for a genuine miss; raise
        SourceUnavailable on transient failure."""
        ...


def _round_to_cents(market_usd: float) -> int:
    return int(round(float(market_usd) * 100))


class PokemonTcgPriceSource:
    """GET {pokemontcg_base}/cards/{id}, read data.tcgplayer.prices[finish].market."""

    name = "pokemontcg"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
    ):
        # An injected client (tests hand in a MockTransport-backed one) is used
        # as-is; otherwise a real client is created lazily so import stays cheap.
        self._client = client
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT)
        return self._client

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        base = cfg["pricing"]["pokemontcg_base"].rstrip("/")
        url = f"{base}/cards/{card_ref_id}"
        headers: dict[str, str] = {}
        api_key = os.environ.get("POKEMONTCG_API_KEY")
        if api_key:
            headers["X-Api-Key"] = api_key   # raises the rate limit; pricing works keyless too

        client = self._get_client()
        delay = self._backoff_base
        for attempt in range(self._max_retries):
            try:
                resp = client.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._max_retries - 1:
                    raise SourceUnavailable(f"pokemontcg transport error: {exc}") from exc
                if delay:
                    time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries - 1:
                    raise SourceUnavailable(f"pokemontcg HTTP {resp.status_code}")
                if delay:
                    time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code >= 400:
                # A permanent 4xx (e.g. 404 for an unknown id) is a genuine miss,
                # not a transient failure — no price data for this card.
                return None

            data = (resp.json() or {}).get("data") or {}
            prices = ((data.get("tcgplayer") or {}).get("prices")) or {}
            entry = prices.get(finish)
            if not entry:
                return None
            market = entry.get("market")
            if market is None:
                return None
            return _round_to_cents(market)

        raise SourceUnavailable("pokemontcg retries exhausted")  # unreachable


class CollectrPriceSource:
    """Stub until COLLECTR_API_KEY is provisioned — always reports 'not configured'
    so resolve_price skips it (plan Global Constraints: Collectr is a stub for M3)."""

    name = "collectr"

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        raise SourceNotConfigured("collectr access pending")


def resolve_price(
    sources: list[PriceSource], card_ref_id: str, finish: str, cfg: dict
) -> tuple[int | None, str]:
    """Try sources in order. The first source that RETURNS (int cents or None,
    without raising) wins -> (cents_or_None, source_name). A SourceUnavailable /
    SourceNotConfigured skips to the next source. If every source skips, re-raise
    the last SourceUnavailable so the caller can retry the job later."""
    last_exc: SourceUnavailable | None = None
    for src in sources:
        try:
            cents = src.fetch(card_ref_id, finish, cfg)
        except SourceUnavailable as exc:
            last_exc = exc
            continue
        return cents, src.name
    raise last_exc or SourceUnavailable("no price sources configured")
