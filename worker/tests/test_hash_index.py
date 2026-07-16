import numpy as np
import pytest

from notbulk.hash_index import HashIndex, to_uint64
from notbulk.types import CropHashes


def _hashes(full, edge=None, art=None, name=None, text=None):
    edge = full if edge is None else edge
    art = full if art is None else art
    name = full if name is None else name
    text = full if text is None else text
    return CropHashes(full=full, edge=edge, region_art=art,
                      region_name=name, region_text=text)


def _rows_for(card, h: CropHashes):
    return [
        (card, "full", h.full),
        (card, "edge", h.edge),
        (card, "region_art", h.region_art),
        (card, "region_name", h.region_name),
        (card, "region_text", h.region_text),
    ]


CFG = {"hash": {"accept_distance": 10, "min_margin": 4}}

# Two well-separated 64-bit hashes (hamming distance 32).
HA = 0x0F0F0F0F0F0F0F0F
HB = 0xF0F0F0F0F0F0F0F0


def test_exact_match_distance_zero_large_margin():
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HB))
    idx = HashIndex.from_rows(rows)
    m = idx.match(_hashes(HA), CFG)
    assert m is not None
    assert m.card_ref_id == "sv4-1"
    assert m.distance == 0
    assert m.agreement == 5
    assert m.margin >= CFG["hash"]["min_margin"]
    assert 0.0 <= m.score <= 1.0


def test_near_hash_matches():
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HB))
    idx = HashIndex.from_rows(rows)
    near = HA ^ 0b111                       # 3 bits off
    m = idx.match(_hashes(near), CFG)
    assert m is not None
    assert m.card_ref_id == "sv4-1"
    assert m.distance == 3


def test_ambiguous_two_cards_same_hash_returns_none_or_low_agreement():
    # Two distinct cards share the identical hash on every tier -> zero margin.
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HA))
    idx = HashIndex.from_rows(rows)
    m = idx.match(_hashes(HA), CFG)
    # Margin (second-distinct minus top on full hash) is 0 < min_margin -> reject.
    assert m is None


def test_match_full_only_returns_best():
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HB))
    idx = HashIndex.from_rows(rows)
    res = idx.match_full_only(HB ^ 0b1)      # 1 bit off HB
    assert res is not None
    card, dist = res
    assert card == "sv4-2"
    assert dist == 1


def test_twos_complement_roundtrip_negative_bigint():
    # A hash with the high bit set is stored as a negative signed bigint.
    signed = -1                              # all 64 bits set as two's complement
    u = to_uint64(signed)
    assert u == 0xFFFFFFFFFFFFFFFF
    rows = _rows_for("sv4-9", _hashes(int(u)))
    idx = HashIndex.from_rows(rows)          # from_rows normalizes via to_uint64 too
    m = idx.match(_hashes(int(u)), CFG)
    assert m is None or m.card_ref_id == "sv4-9"  # single card -> no margin, allowed to reject


def test_twos_complement_roundtrip_other_negative_values():
    # A few more representative signed bigints, including a mid-range negative
    # value (not just all-bits-set) to exercise the two's-complement mask.
    cases = {
        -1: 0xFFFFFFFFFFFFFFFF,
        -2: 0xFFFFFFFFFFFFFFFE,
        -9223372036854775808: 0x8000000000000000,  # INT64_MIN
        0: 0x0000000000000000,
        9223372036854775807: 0x7FFFFFFFFFFFFFFF,     # INT64_MAX
    }
    for signed, expected in cases.items():
        assert to_uint64(signed) == expected


def test_len_reflects_loaded_entries():
    # Empty index reports zero; used by CLI/eval to detect an unbuilt index.
    assert len(HashIndex.from_rows([])) == 0
    # Two cards x 5 hash tiers = 10 total entries.
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HB))
    assert len(HashIndex.from_rows(rows)) == 10


def test_popcount_lookup_table_fallback_matches_bit_count():
    # Assembly Resolution: the 16-bit lookup-table fallback (used when
    # np.bitwise_count is unavailable) must be directly unit-tested, not
    # merely exercised via the runtime branch -- this environment has
    # NumPy 2.4.6 where np.bitwise_count exists, so the branch alone would
    # never hit the fallback path.
    from notbulk.hash_index import _popcount_lookup_table

    values = np.array(
        [0, 1, 0xFFFFFFFFFFFFFFFF, HA, HB, HA ^ HB, 0x8000000000000000],
        dtype=np.uint64,
    )
    result = _popcount_lookup_table(values)
    expected = np.array([bin(int(v)).count("1") for v in values], dtype=np.uint32)
    np.testing.assert_array_equal(result, expected)
