import os

import numpy as np
import cv2
import uuid
import pytest

from scripts.build_hash_index import (
    letterbox,
    hash_rows_for_card,
    to_signed_bigint,
    stable_seed,
    delete_sql,
)

CFG = {
    "crop": {"width": 734, "height": 1024, "webp_quality": 80},
    "hash": {"accept_distance": 10, "min_margin": 4, "augmentations_per_card": 6},
}


def _flat_scan():
    """A flat reference-scan-like image (arbitrary source resolution)."""
    img = np.zeros((600, 430, 3), dtype=np.uint8)      # ~card aspect, wrong size
    img[:] = np.repeat(np.linspace(10, 240, 430, dtype=np.uint8)[None, :, None], 600, axis=0)
    return img


def test_letterbox_hits_canonical_size():
    out = letterbox(_flat_scan(), CFG)
    assert out.shape == (1024, 734, 3)
    assert out.dtype == np.uint8


def test_row_counts_and_types():
    rows = hash_rows_for_card("sv4-123", _flat_scan(), CFG)
    n = CFG["hash"]["augmentations_per_card"]
    assert len(rows) == 5 + 5 * n                       # 5 reference + 5*n augmented
    sources = [r[4] for r in rows]
    assert sources.count("reference") == 5
    assert sources.count("augmented") == 5 * n
    assert "user_validated" not in sources              # never generated here
    # Every row has a distinct uuid id and a valid hash_type.
    valid_types = {"full", "edge", "region_art", "region_name", "region_text"}
    assert {r[2] for r in rows} == valid_types
    assert len({r[0] for r in rows}) == len(rows)       # unique ids
    for r in rows:
        uuid.UUID(str(r[0]))                             # parses as a uuid


def test_reference_rows_carry_all_five_hash_types_once():
    rows = hash_rows_for_card("sv4-1", _flat_scan(), CFG)
    ref = [r for r in rows if r[4] == "reference"]
    assert sorted(r[2] for r in ref) == [
        "edge", "full", "region_art", "region_name", "region_text",
    ]


def test_hash_bits_stored_as_signed_bigint():
    # A uint64 with the high bit set must round-trip to a negative signed bigint.
    u = 0xFFFFFFFFFFFFFFFF
    s = to_signed_bigint(u)
    assert s == -1
    assert -(2 ** 63) <= s <= (2 ** 63) - 1             # fits Postgres bigint
    # Every generated hash_bits is a valid signed bigint.
    for r in hash_rows_for_card("sv4-2", _flat_scan(), CFG):
        assert -(2 ** 63) <= r[3] <= (2 ** 63) - 1


def test_stable_seed_is_deterministic_per_card():
    assert stable_seed("sv4-77") == stable_seed("sv4-77")
    assert stable_seed("sv4-77") != stable_seed("sv4-78")


def test_delete_sql_scopes_out_user_validated():
    sql = delete_sql()
    low = sql.lower()
    assert "delete from ref_hashes" in low
    assert "source in ('reference','augmented')" in low.replace(" ", "").replace(
        "\n", ""
    ).replace("sourcein", "source in").replace("in(", "in (")
    assert "user_validated" not in low                  # must never delete validated rows
    assert "card_ref_id = any(" in low                  # batched by card id array


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (run under `bws run` for the integration path)",
)
def test_idempotent_rebuild_against_real_db():
    """Insert a synthetic card_refs row, run the row-generation + insert +
    idempotent-rebuild cycle against the real local DB, then clean up.

    Verifies: rows land with the expected counts/sources, a re-run does not
    duplicate them (idempotent DELETE-then-insert scoped to
    ('reference','augmented')), and a pre-existing 'user_validated' row for
    the same card survives both runs untouched.
    """
    from notbulk.db import get_pool

    pool = get_pool()
    test_card_id = f"test-build-hash-index-{uuid.uuid4().hex[:8]}"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO card_refs
                  (id, name, set_id, set_name, number, printed_total, rarity,
                   image_url, finishes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    test_card_id,
                    "Integration Test Card",
                    "test-set",
                    "Test Set",
                    "1",
                    "1",
                    "common",
                    "https://example.invalid/card.png",
                    [],
                ),
            )
            # A pre-existing user_validated row that must survive every rebuild.
            validated_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO ref_hashes (id, card_ref_id, hash_type, hash_bits, source)
                VALUES (%s, %s, 'full', %s, 'user_validated')
                """,
                (validated_id, test_card_id, to_signed_bigint(0x1234)),
            )
        conn.commit()

    try:
        rows = hash_rows_for_card(test_card_id, _flat_scan(), CFG)
        n = CFG["hash"]["augmentations_per_card"]
        expected_generated = 5 + 5 * n

        def _rebuild():
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(delete_sql(), ([test_card_id],))
                    cur.executemany(
                        "INSERT INTO ref_hashes "
                        "(id, card_ref_id, hash_type, hash_bits, source) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        rows,
                    )
                conn.commit()

        def _counts():
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT source, count(*) FROM ref_hashes "
                        "WHERE card_ref_id = %s GROUP BY source",
                        (test_card_id,),
                    )
                    return dict(cur.fetchall())

        _rebuild()
        counts_first = _counts()
        assert counts_first.get("reference") == 5
        assert counts_first.get("augmented") == 5 * n
        assert counts_first.get("user_validated") == 1

        _rebuild()  # idempotent re-run
        counts_second = _counts()
        assert counts_second == counts_first  # no growth, no duplication

        # The originally-inserted user_validated row itself is untouched.
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM ref_hashes WHERE source = 'user_validated' "
                    "AND card_ref_id = %s",
                    (test_card_id,),
                )
                assert [r[0] for r in cur.fetchall()] == [uuid.UUID(validated_id)]
    finally:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM card_refs WHERE id = %s", (test_card_id,))
            conn.commit()
