"""Method C: Anthropic vision tiebreaker with content-hash cache (design A6).

Cache key = sha256 of the exact WebP bytes sent to the API (NOT pHash). Cache
hits skip the API entirely. Malformed responses score 0 and are never cached.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

import cv2
import numpy as np

from notbulk.ocr import resolve  # REUSE the single name/number resolver
from notbulk.types import MethodResult

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

PROMPT = (
    "You are identifying a single Pokemon trading card from its cropped image. "
    "Return the printed card name, any set symbol/abbreviation you can read, the "
    "collector number exactly as printed (e.g. '25/185' or a promo code like "
    "'SWSH039'), and your confidence 0-100. If a field is unreadable use null. "
    'Respond with ONLY the JSON object, no prose:\n'
    '{"name": string|null, "set_hint": string|null, '
    '"number": string|null, "confidence": integer 0-100}'
)


def _encode_webp(crop_bgr: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".webp", crop_bgr, [cv2.IMWRITE_WEBP_QUALITY, quality])
    if not ok:
        raise ValueError("WebP encode failed")
    return buf.tobytes()


def _crop_key(crop_bgr: np.ndarray, quality: int) -> str:
    return hashlib.sha256(_encode_webp(crop_bgr, quality)).hexdigest()


def _parse_response(text: str) -> dict | None:
    """Extract and parse the first {...} JSON block. None on any failure."""
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _score_from(parsed: dict, pool) -> MethodResult:
    """Resolve name/number -> card_ref_id; score = confidence/100 * exactness."""
    name = parsed.get("name")
    number = parsed.get("number")
    card_ref_id, exactness = resolve(pool, name, number)
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    score = (confidence / 100.0) * exactness
    return MethodResult(method="c", card_ref_id=card_ref_id, score=score)


def llm_match(client, pool, crop_bgr: np.ndarray, cfg: dict) -> MethodResult:
    quality = cfg["crop"]["webp_quality"]
    webp = _encode_webp(crop_bgr, quality)
    key = hashlib.sha256(webp).hexdigest()
    model = cfg["models"]["llm"]

    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT response FROM llm_cache WHERE crop_sha256 = %s", (key,))
        row = cur.fetchone()
        if row is not None:
            cached_raw = row[0]
            parsed = cached_raw if isinstance(cached_raw, dict) else _parse_response(cached_raw)
            if parsed is None:
                return MethodResult(method="c", card_ref_id=None, score=0.0)
            return _score_from(parsed, pool)

    # ---- cache miss: call the API ----
    return _call_and_cache(client, pool, crop_bgr, webp, key, model, cfg)


def _call_and_cache(client, pool, crop_bgr, webp: bytes, key: str, model: str,
                    cfg: dict) -> MethodResult:
    b64 = base64.b64encode(webp).decode("ascii")
    message = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/webp",
                        "data": b64,
                    },
                },
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    raw_text = message.content[0].text if message.content else ""
    parsed = _parse_response(raw_text)
    if parsed is None:
        # Do NOT cache failures (design A6).
        return MethodResult(method="c", card_ref_id=None, score=0.0)

    # Resolve before caching: keeps the resolve()-then-INSERT query order
    # (cache SELECT, resolve lookup, INSERT) that concurrent workers observe.
    result = _score_from(parsed, pool)

    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO llm_cache (crop_sha256, model, response) "
            "VALUES (%s, %s, %s) ON CONFLICT (crop_sha256) DO NOTHING",
            (key, model, json.dumps(parsed)),
        )

    return result
