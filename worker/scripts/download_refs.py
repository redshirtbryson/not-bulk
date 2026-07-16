"""Download pokemontcg.io reference cards into card_refs + mirror images locally.

Run under `bws run` so POKEMONTCG_API_KEY and DATABASE_URL are injected:
    bws run -- uv run python scripts/download_refs.py --sets sv4
    bws run -- uv run python scripts/download_refs.py            # full ~20k catalog
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

from notbulk.db import get_pool

API_BASE = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE = 250
REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
MAX_CONCURRENCY = 4
POLITE_DELAY = 0.05          # seconds between image fetches
MAX_RETRIES = 5


def card_to_row(card: dict) -> tuple:
    """Map a /v2/cards data[] entry to a card_refs row tuple.

    Returns: (id, name, set_id, set_name, number, printed_total, rarity, image_url, finishes)
    """
    card_set = card.get("set", {})
    printed_total = card_set.get("printedTotal")
    prices = (card.get("tcgplayer") or {}).get("prices") or {}
    finishes = sorted(prices.keys())
    return (
        card["id"],
        card["name"],
        card_set.get("id", ""),
        card_set.get("name", ""),
        card["number"],
        str(printed_total) if printed_total is not None else None,
        card.get("rarity"),
        card["images"]["large"],
        finishes,
    )


def needs_download(path: Path) -> bool:
    """True if the image is missing or zero-length (resume-skip)."""
    try:
        return path.stat().st_size == 0
    except FileNotFoundError:
        return True


def _upsert_rows(pool, rows: list[tuple]) -> None:
    sql = """
        INSERT INTO card_refs
          (id, name, set_id, set_name, number, printed_total, rarity, image_url, finishes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
          name = EXCLUDED.name,
          set_id = EXCLUDED.set_id,
          set_name = EXCLUDED.set_name,
          number = EXCLUDED.number,
          printed_total = EXCLUDED.printed_total,
          rarity = EXCLUDED.rarity,
          image_url = EXCLUDED.image_url,
          finishes = EXCLUDED.finishes,
          synced_at = now()
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


async def _get_page(client: httpx.AsyncClient, params: dict) -> dict:
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.get(API_BASE, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                resp.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


async def _download_image(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, card_id: str, url: str,
    skipped: list[tuple[str, str]] | None = None,
) -> None:
    """Fetch one reference image. Retries only transient failures (429 and
    5xx/transport errors); a permanent 4xx is logged and recorded in
    `skipped`, never raised — pokemontcg.io has genuine 404s in its catalog,
    and one dead URL must not abort the whole asyncio.gather batch. The
    builders (build_hash_index.py, build_embed_index.py) already tolerate a
    missing local scan for a card."""
    dest = REFS_DIR / f"{card_id}.png"
    if not needs_download(dest):
        return
    async with sem:
        delay = 1.0
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(url)
            except httpx.TransportError:
                if attempt == MAX_RETRIES - 1:
                    print(f"skip {card_id}: transport error (retries exhausted)",
                          file=sys.stderr)
                    if skipped is not None:
                        skipped.append((card_id, "transport error"))
                    return
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    print(f"skip {card_id}: HTTP {resp.status_code} (retries exhausted)",
                          file=sys.stderr)
                    if skipped is not None:
                        skipped.append((card_id, f"HTTP {resp.status_code}"))
                    return
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code >= 400:
                # Permanent client error (e.g. 404) — never retry, never raise
                # out of the gather; just record and move on.
                print(f"skip {card_id}: HTTP {resp.status_code}", file=sys.stderr)
                if skipped is not None:
                    skipped.append((card_id, f"HTTP {resp.status_code}"))
                return

            tmp = dest.with_suffix(".png.part")
            tmp.write_bytes(resp.content)
            tmp.rename(dest)
            break
        await asyncio.sleep(POLITE_DELAY)


async def run(set_ids: list[str] | None) -> None:
    api_key = os.environ.get("POKEMONTCG_API_KEY")
    if not api_key:
        raise RuntimeError(
            "POKEMONTCG_API_KEY is not set; run under `bws run`"
        )
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    pool = get_pool()

    query = None
    if set_ids:
        query = " OR ".join(f"set.id:{s}" for s in set_ids)

    total = 0
    skipped: list[tuple[str, str]] = []
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        page = 1
        while True:
            params = {"page": page, "pageSize": PAGE_SIZE}
            if query:
                params["q"] = query
            data = await _get_page(client, params)
            cards = data.get("data", [])
            if not cards:
                break

            rows = [card_to_row(c) for c in cards]
            _upsert_rows(pool, rows)

            await asyncio.gather(
                *(
                    _download_image(client, sem, c["id"], c["images"]["large"], skipped)
                    for c in cards
                )
            )

            total += len(cards)
            if total % 100 < PAGE_SIZE:
                print(f"...{total} cards synced (page {page})", flush=True)

            if len(cards) < PAGE_SIZE:
                break
            page += 1

    print(f"done: {total} cards synced into card_refs, images mirrored to {REFS_DIR}")
    if skipped:
        print(f"skipped {len(skipped)} image download(s) (permanent errors, catalog synced anyway):")
        for card_id, reason in skipped:
            print(f"  {card_id}: {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download pokemontcg.io reference cards")
    parser.add_argument(
        "--sets",
        nargs="*",
        default=None,
        help="limit to specific set ids (e.g. --sets sv4 sv3) for dev/testing",
    )
    args = parser.parse_args(argv)
    asyncio.run(run(args.sets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
