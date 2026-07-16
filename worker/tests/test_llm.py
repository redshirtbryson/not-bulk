import hashlib
import json

import cv2
import numpy as np
import pytest

from notbulk.llm import llm_match, _encode_webp, _crop_key
from tests.fakes import FakePool, FakeAnthropic

CFG = {"models": {"llm": "claude-haiku-4-5-20251001"}, "crop": {"webp_quality": 80}}


def _crop():
    # Non-uniform image so WebP encoding is stable and non-trivial.
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (1024, 734, 3), dtype=np.uint8)


def test_crop_key_is_sha256_of_webp_bytes_not_raw_array():
    crop = _crop()
    webp = _encode_webp(crop, 80)
    expected = hashlib.sha256(webp).hexdigest()
    assert _crop_key(crop, 80) == expected
    # And it must NOT equal a hash of the raw array bytes.
    assert _crop_key(crop, 80) != hashlib.sha256(crop.tobytes()).hexdigest()


def test_cache_hit_skips_client_entirely():
    crop = _crop()
    key = _crop_key(crop, 80)
    cached = json.dumps({"name": "Charizard", "set_hint": "sv4", "number": "25/185",
                         "confidence": 88})
    # HIT: first query (SELECT response) returns the row.
    # Then resolve() runs its own query (number match) -> unique row.
    pool = FakePool([[(cached,)], [("sv4-25", "Charizard")]])
    client = FakeAnthropic(raise_if_called=True)  # must never be called
    r = llm_match(client, pool, crop, CFG)
    assert r.method == "c"
    assert r.card_ref_id == "sv4-25"
    # score = confidence/100 * exactness(1.0) = 0.88
    assert abs(r.score - 0.88) < 1e-6
    assert client.messages.calls == []  # proven no API call


def test_cache_miss_calls_api_and_caches_success():
    crop = _crop()
    key = _crop_key(crop, 80)
    good = json.dumps({"name": "Pikachu", "set_hint": "base", "number": "58/102",
                       "confidence": 95})
    # MISS: SELECT returns no row; resolve() number query -> unique; INSERT runs.
    pool = FakePool([[], [("base1-58", "Pikachu")], []])
    client = FakeAnthropic(canned_text=good)
    r = llm_match(client, pool, crop, CFG)
    assert r.method == "c"
    assert r.card_ref_id == "base1-58"
    assert abs(r.score - 0.95) < 1e-6
    assert len(client.messages.calls) == 1
    # The image block must carry base64 WebP with the exact key bytes.
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert sent["max_tokens"] == 300
    # Assert an INSERT into llm_cache was issued keyed by the sha256.
    inserts = [e for e in pool.cursor.executed if "INSERT" in e[0].upper()]
    assert inserts and key in inserts[0][1]


def test_malformed_json_scores_zero_and_is_not_cached():
    crop = _crop()
    pool = FakePool([[]])  # MISS; no resolve query, no INSERT expected
    client = FakeAnthropic(canned_text="I think this is a Charizard, sorry!")
    r = llm_match(client, pool, crop, CFG)
    assert r.method == "c"
    assert r.card_ref_id is None
    assert r.score == 0.0
    assert len(client.messages.calls) == 1
    inserts = [e for e in pool.cursor.executed if "INSERT" in e[0].upper()]
    assert inserts == []  # failure never cached
