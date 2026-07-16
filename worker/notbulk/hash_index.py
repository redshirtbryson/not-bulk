"""In-memory hash index and vectorized ensemble matcher (design A9).

`ref_hashes` (Postgres) is the durable source of truth; this class is the
rebuilt-on-start lookup artifact. Each of the 5 hash types gets a parallel
pair of uint64 arrays: hash bits and the owning card_ref_id (as an index into
a card table). Matching is XOR + vectorized popcount, no BK-tree needed at
this scale.
"""
from __future__ import annotations

import numpy as np

from .types import CropHashes, HashMatch

_HASH_TYPES = ("full", "edge", "region_art", "region_name", "region_text")

# Agreement is counted over hash types 0-5, i.e. all 5 tiers above; a
# candidate is only returned by match() when at least 3 of them agree.
_MIN_AGREEMENT = 3

# Precomputed 16-bit popcount table: distance of each uint64 = sum of popcounts
# of its four 16-bit shorts. Used when numpy lacks np.bitwise_count.
_POPCOUNT16 = np.array(
    [bin(i).count("1") for i in range(1 << 16)], dtype=np.uint8
)

_HAS_BITCOUNT = hasattr(np, "bitwise_count")


def to_uint64(x: int) -> np.uint64:
    """Interpret a signed Python int (possibly a negative signed bigint from
    Postgres) as an unsigned 64-bit value via two's complement."""
    return np.uint64(int(x) & 0xFFFFFFFFFFFFFFFF)


def _popcount_lookup_table(arr: np.ndarray) -> np.ndarray:
    """16-bit lookup-table popcount fallback for a uint64 array.

    Split each uint64 into four 16-bit shorts and sum table lookups. Used
    when np.bitwise_count is unavailable (numpy < 2). Kept as a standalone,
    directly-testable function rather than inlined in the runtime branch.
    """
    a = arr.view(np.uint64)
    total = np.zeros(a.shape, dtype=np.uint32)
    for shift in (0, 16, 32, 48):
        shorts = ((a >> np.uint64(shift)) & np.uint64(0xFFFF)).astype(np.uint16)
        total += _POPCOUNT16[shorts].astype(np.uint32)
    return total


def _popcount(arr: np.ndarray) -> np.ndarray:
    """Vectorized popcount over a uint64 array -> uint32 distances."""
    if _HAS_BITCOUNT:
        return np.bitwise_count(arr).astype(np.uint32)   # numpy >= 2
    return _popcount_lookup_table(arr)


class HashIndex:
    def __init__(self, cards: list[str], bits: dict[str, np.ndarray],
                 owners: dict[str, np.ndarray]):
        self._cards = cards                              # card_ref_id by owner index
        self._bits = bits                                # hash_type -> uint64 array
        self._owners = owners                            # hash_type -> int32 owner-index array

    def __len__(self) -> int:
        """Total hash entries loaded across all tiers; 0 means the index is unbuilt."""
        return sum(int(a.size) for a in self._bits.values())

    @classmethod
    def from_rows(cls, rows: list[tuple[str, str, int]]) -> "HashIndex":
        """Build from (card_ref_id, hash_type, hash_bits) rows."""
        card_to_idx: dict[str, int] = {}
        cards: list[str] = []
        per_type_bits: dict[str, list[int]] = {t: [] for t in _HASH_TYPES}
        per_type_owner: dict[str, list[int]] = {t: [] for t in _HASH_TYPES}
        for card_ref_id, hash_type, hash_bits in rows:
            if hash_type not in per_type_bits:
                continue
            if card_ref_id not in card_to_idx:
                card_to_idx[card_ref_id] = len(cards)
                cards.append(card_ref_id)
            per_type_bits[hash_type].append(int(to_uint64(hash_bits)))
            per_type_owner[hash_type].append(card_to_idx[card_ref_id])
        bits = {t: np.array(per_type_bits[t], dtype=np.uint64) for t in _HASH_TYPES}
        owners = {t: np.array(per_type_owner[t], dtype=np.int32) for t in _HASH_TYPES}
        return cls(cards, bits, owners)

    @classmethod
    def load(cls, pool) -> "HashIndex":
        """Rebuild the index from ref_hashes. bigint -> uint64 two's complement."""
        rows: list[tuple[str, str, int]] = []
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT card_ref_id, hash_type, hash_bits FROM ref_hashes"
                )
                for card_ref_id, hash_type, hash_bits in cur:
                    rows.append((card_ref_id, hash_type, int(hash_bits)))
        return cls.from_rows(rows)

    def _best_two_distinct(self, query_bits: np.uint64, hash_type: str):
        """Return (top_card_idx, top_dist, second_distinct_dist) for one tier."""
        table = self._bits[hash_type]
        owners = self._owners[hash_type]
        if table.size == 0:
            return None, 64, 64
        dist = _popcount(table ^ np.uint64(query_bits))
        order = np.argsort(dist, kind="stable")
        top_owner = owners[order[0]]
        top_dist = int(dist[order[0]])
        second_dist = 64
        for i in order[1:]:
            if owners[i] != top_owner:
                second_dist = int(dist[i])
                break
        return int(top_owner), top_dist, second_dist

    def match(self, h: CropHashes, cfg: dict) -> HashMatch | None:
        """Ensemble match across all 5 tiers.

        Agreement = number of hash types 0-5 (full, edge, region_art,
        region_name, region_text) where a card is the top hit within
        accept_distance. margin is computed on the full-hash tier only (gap
        to the second-best DISTINCT card). Accept requires agreement>=3 AND
        full-hash top distance<=accept_distance AND full-hash margin>=min_margin.
        """
        accept = int(cfg["hash"]["accept_distance"])
        min_margin = int(cfg["hash"]["min_margin"])
        query = {
            "full": to_uint64(h.full),
            "edge": to_uint64(h.edge),
            "region_art": to_uint64(h.region_art),
            "region_name": to_uint64(h.region_name),
            "region_text": to_uint64(h.region_text),
        }

        # Full-hash tier drives distance and margin.
        full_owner, full_dist, full_second = self._best_two_distinct(
            query["full"], "full"
        )
        if full_owner is None:
            return None
        candidate_card = self._cards[full_owner]

        # Agreement: how many tiers rank candidate_card top within accept.
        agreement = 0
        for t in _HASH_TYPES:
            owner, dist, _ = self._best_two_distinct(query[t], t)
            if owner is not None and self._cards[owner] == candidate_card and dist <= accept:
                agreement += 1

        margin = full_second - full_dist
        if not (agreement >= _MIN_AGREEMENT and full_dist <= accept and margin >= min_margin):
            return None

        score = _score(full_dist, margin, agreement, accept)
        return HashMatch(
            card_ref_id=candidate_card,
            score=score,
            distance=full_dist,
            margin=margin,
            agreement=agreement,
        )

    def match_full_only(self, full_hash: int) -> tuple[str, int] | None:
        """Best (card_ref_id, distance) on the full-hash tier only."""
        owner, dist, _ = self._best_two_distinct(to_uint64(full_hash), "full")
        if owner is None:
            return None
        return self._cards[owner], dist


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score(distance: int, margin: int, agreement: int, accept: int) -> float:
    """Composite Stage-1 score in [0,1]."""
    return _clip01(
        0.5 * (1 - distance / accept)
        + 0.3 * min(margin / 10.0, 1.0)
        + 0.2 * (agreement / 5.0)
    )
