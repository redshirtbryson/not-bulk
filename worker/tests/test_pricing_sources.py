"""PokemonTcgPriceSource (httpx.MockTransport, no network), the Collectr stub,
and resolve_price ordering. No real network, no DB."""
from __future__ import annotations

import httpx
import pytest

from notbulk.pricing import sources as S

CFG = {"pricing": {"pokemontcg_base": "https://api.pokemontcg.io/v2"}}

# A real /v2/cards/{id} response shape: data.tcgplayer.prices[finish].market (USD float).
_CARD_JSON = {
    "data": {
        "id": "sv4-123",
        "name": "Charizard ex",
        "tcgplayer": {
            "prices": {
                "normal": {"market": 1.20},
                "holofoil": {"market": 12.34},
            }
        },
    }
}
_CARD_NO_HOLO = {
    "data": {"id": "sv4-5", "name": "Pikachu", "tcgplayer": {"prices": {"normal": {"market": 0.10}}}}
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pokemontcg_holofoil_market_to_cents():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/cards/sv4-123"
        return httpx.Response(200, json=_CARD_JSON, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-123", "holofoil", CFG) == 1234   # 12.34 USD -> 1234 cents


def test_pokemontcg_missing_finish_key_is_a_miss():
    def handler(request):
        return httpx.Response(200, json=_CARD_NO_HOLO, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-5", "holofoil", CFG) is None      # finish key absent -> None (genuine miss)


def test_pokemontcg_missing_market_field_is_a_miss():
    def handler(request):
        body = {"data": {"tcgplayer": {"prices": {"holofoil": {"low": 5.0}}}}}   # no 'market'
        return httpx.Response(200, json=body, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-123", "holofoil", CFG) is None


def test_pokemontcg_no_tcgplayer_block_is_a_miss():
    def handler(request):
        return httpx.Response(200, json={"data": {"id": "x"}}, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("x", "normal", CFG) is None


def test_pokemontcg_429_raises_source_unavailable():
    def handler(request):
        return httpx.Response(429, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_500_raises_source_unavailable():
    def handler(request):
        return httpx.Response(503, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_transport_error_raises_source_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_sends_api_key_header_when_env_set(monkeypatch):
    monkeypatch.setenv("POKEMONTCG_API_KEY", "secret-key")
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json=_CARD_JSON, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    src.fetch("sv4-123", "holofoil", CFG)
    assert seen["key"] == "secret-key"


def test_collectr_stub_raises_not_configured():
    src = S.CollectrPriceSource()
    with pytest.raises(S.SourceNotConfigured):
        src.fetch("sv4-123", "holofoil", CFG)


def test_resolve_price_skips_unconfigured_uses_next():
    def ok_handler(request):
        return httpx.Response(200, json=_CARD_JSON, request=request)

    ordered = [S.CollectrPriceSource(), S.PokemonTcgPriceSource(client=_client(ok_handler))]
    cents, name = S.resolve_price(ordered, "sv4-123", "holofoil", CFG)
    assert (cents, name) == (1234, "pokemontcg")   # collectr skipped, pokemontcg used


def test_resolve_price_returns_genuine_miss_from_first_working_source():
    def miss_handler(request):
        return httpx.Response(200, json=_CARD_NO_HOLO, request=request)

    ordered = [S.PokemonTcgPriceSource(client=_client(miss_handler))]
    cents, name = S.resolve_price(ordered, "sv4-5", "holofoil", CFG)
    assert (cents, name) == (None, "pokemontcg")   # a returned None (no raise) wins immediately


def test_resolve_price_all_unavailable_raises():
    def down_handler(request):
        return httpx.Response(500, request=request)

    ordered = [
        S.CollectrPriceSource(),
        S.PokemonTcgPriceSource(client=_client(down_handler), max_retries=1, backoff_base=0),
    ]
    with pytest.raises(S.SourceUnavailable):
        S.resolve_price(ordered, "sv4-123", "holofoil", CFG)
