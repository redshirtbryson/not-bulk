import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scripts.download_refs import _download_image, card_to_row, needs_download

FIXTURE = Path(__file__).parent / "fixtures" / "pokemontcg_page.json"


def _load_cards():
    return json.loads(FIXTURE.read_text())["data"]


def test_card_to_row_full_mapping():
    charizard, _ = _load_cards()
    row = card_to_row(charizard)
    # (id, name, set_id, set_name, number, printed_total, rarity, image_url, finishes)
    assert row == (
        "sv4-123",
        "Charizard ex",
        "sv4",
        "Paradox Rift",
        "123",
        "182",
        "Double Rare",
        "https://images.pokemontcg.io/sv4/123_hires.png",
        ["holofoil", "normal"],
    )


def test_card_to_row_handles_missing_optional_fields():
    _, pikachu = _load_cards()
    row = card_to_row(pikachu)
    assert row[0] == "sv4-5"
    assert row[5] == "182"     # printed_total coerced to text
    assert row[6] is None      # rarity absent
    assert row[7] == "https://images.pokemontcg.io/sv4/5_hires.png"
    assert row[8] == []        # no tcgplayer prices -> empty finishes


def test_needs_download_true_when_missing(tmp_path):
    assert needs_download(tmp_path / "sv4-123.png") is True


def test_needs_download_false_when_present_and_nonempty(tmp_path):
    p = tmp_path / "sv4-123.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n_fake_image_bytes")
    assert needs_download(p) is False


def test_needs_download_true_when_present_but_empty(tmp_path):
    p = tmp_path / "sv4-123.png"
    p.write_bytes(b"")
    assert needs_download(p) is True


def test_download_image_404_returns_without_raising_and_records_skip(tmp_path, monkeypatch):
    """A permanent 4xx must never retry and never raise out of the gather —
    it logs `skip {id}: HTTP {status}`, leaves no file, and is recorded."""
    monkeypatch.setattr("scripts.download_refs.REFS_DIR", tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)
    skipped: list[tuple[str, str]] = []

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            sem = asyncio.Semaphore(1)
            await _download_image(client, sem, "sv4-404", "https://example.invalid/x.png", skipped)

    asyncio.run(_run())

    assert calls["n"] == 1  # never retried
    assert not (tmp_path / "sv4-404.png").exists()
    assert skipped == [("sv4-404", "HTTP 404")]


def test_download_image_429_then_200_retries_and_writes(tmp_path, monkeypatch):
    """A 429 is transient and must be retried; a subsequent 200 writes the file."""
    monkeypatch.setattr("scripts.download_refs.REFS_DIR", tmp_path)
    monkeypatch.setattr("scripts.download_refs.POLITE_DELAY", 0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, content=b"fake-png-bytes", request=request)

    transport = httpx.MockTransport(handler)
    skipped: list[tuple[str, str]] = []

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            sem = asyncio.Semaphore(1)
            # patch sleep so the exponential backoff doesn't slow the test down
            import scripts.download_refs as mod
            orig_sleep = asyncio.sleep
            async def _fast_sleep(_secs):
                await orig_sleep(0)
            mod.asyncio.sleep = _fast_sleep
            try:
                await _download_image(client, sem, "sv4-429", "https://example.invalid/x.png", skipped)
            finally:
                mod.asyncio.sleep = orig_sleep

    asyncio.run(_run())

    assert calls["n"] == 2
    assert (tmp_path / "sv4-429.png").read_bytes() == b"fake-png-bytes"
    assert skipped == []
