"""Method B: OCR (PaddleOCR PP-OCRv4) name + collector-number resolution.

Design A5: scan the bottom third for the number pattern; name band as specced.
The paddleocr import is lazy (inside _ensure_engine) so importing this module is
cheap and tests can inject a fake engine.
"""
from __future__ import annotations

import difflib
import re

import cv2
import numpy as np

from notbulk.types import MethodResult

# Fractions of the 734x1024 crop (x0, y0, x1, y1).
NAME_BAND = (0.05, 0.03, 0.80, 0.11)   # top name band, matches hashing REGIONS['name']
BOTTOM_THIRD = (0.0, 0.66, 1.0, 1.0)   # design A5: bottom third, not a tight box

_NUMBER_SLASH = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")
_NUMBER_PROMO = re.compile(r"([A-Z]{2,4}\d{1,3})")
# HP run: digits adjacent to the HP token ('170 HP', 'HP 60') or bare 'HP'.
# Only these digits are noise — names like 'Porygon2' or 'Zygarde 10% Forme'
# keep their digits.
_HP_RUN = re.compile(
    r"(?:\b\d+\s*HP\b|\bHP\s*\d+\b|\bHP\b)", flags=re.IGNORECASE
)


def parse_number(text: str) -> str | None:
    """First 'NNN/NNN' match wins; else first promo 'XX###' match; else None."""
    m = _NUMBER_SLASH.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = _NUMBER_PROMO.search(text)
    if m:
        return m.group(1)
    return None


def clean_name(text: str) -> str:
    """Strip the HP run (and only its adjacent digits) from a name line.

    All other digits are significant ('Porygon2', 'Zygarde 10% Forme').
    """
    stripped = _HP_RUN.sub(" ", text)
    return re.sub(r"\s+", " ", stripped).strip()


def _crop_band(crop_bgr: np.ndarray, band: tuple[float, float, float, float]) -> np.ndarray:
    h, w = crop_bgr.shape[:2]
    x0, y0, x1, y1 = band
    return crop_bgr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _upscale_2x(img: np.ndarray) -> np.ndarray:
    return cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


class OcrReader:
    """Lazy wrapper around PaddleOCR. Inject `engine` in tests to avoid paddle."""

    def __init__(self, engine=None):
        self._engine = engine  # None -> lazily construct the real engine

    def _ensure_engine(self):
        if self._engine is None:
            # Heavy import stays inside the method so module import is light.
            from paddleocr import PaddleOCR

            self._engine = PaddleOCR(
                use_angle_cls=False, lang="en", show_log=False
            )
        return self._engine

    def _run(self, band_img: np.ndarray) -> list[tuple[str, float]]:
        engine = self._ensure_engine()
        upscaled = _upscale_2x(band_img)
        pages = engine.ocr(upscaled, cls=False)
        lines: list[tuple[str, float]] = []
        for page in pages or []:
            for entry in page or []:
                # entry = [box, (text, confidence)]
                text, conf = entry[1]
                lines.append((text, float(conf)))
        return lines

    def read_regions(
        self, crop_bgr: np.ndarray
    ) -> tuple[str | None, str | None, float]:
        name_lines = self._run(_crop_band(crop_bgr, NAME_BAND))
        text_lines = self._run(_crop_band(crop_bgr, BOTTOM_THIRD))

        name = None
        confs: list[float] = []
        if name_lines:
            best_text, best_conf = max(name_lines, key=lambda t: t[1])
            cleaned = clean_name(best_text)
            name = cleaned or None
            confs.append(best_conf)

        joined = " ".join(t for t, _ in text_lines)
        number = parse_number(joined)
        confs.extend(c for _, c in text_lines)

        mean_conf = float(np.mean(confs)) if confs else 0.0
        return name, number, mean_conf


_NM = re.compile(r"^(\d{1,3})\s*/\s*(\d{1,3})$")
_NAME_SIM_MIN = 0.75


def resolve(pool, name: str | None, number: str | None) -> tuple[str | None, float]:
    """Resolve OCR (name, number) to (card_ref_id, exactness 0-1).

    Tiers:
      1.0 : number 'N/M' + printed_total match, exactly one row
      0.9 : number 'N/M' match, multiple rows narrowed by difflib name ratio >=0.75
      0.6 : name-only exact lower() match, unique
      0.0 : nothing
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            nm = _NM.match(number.strip()) if number else None
            if nm:
                # Slash form only: OCR may zero-pad ('012/198') but card_refs
                # stores '12'. Promo codes never reach here — their zeros are
                # significant.
                num = nm.group(1).lstrip("0") or "0"
                total = nm.group(2).lstrip("0") or "0"
                cur.execute(
                    "SELECT id, name FROM card_refs WHERE number = %s AND printed_total = %s",
                    (num, total),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    return rows[0][0], 1.0
                if len(rows) > 1 and name:
                    best_id, best_ratio = None, 0.0
                    for row_id, row_name in rows:
                        ratio = difflib.SequenceMatcher(
                            None, name.lower(), (row_name or "").lower()
                        ).ratio()
                        if ratio > best_ratio:
                            best_id, best_ratio = row_id, ratio
                    if best_ratio >= _NAME_SIM_MIN:
                        return best_id, 0.9

            if name:
                cur.execute(
                    "SELECT id FROM card_refs WHERE lower(name) = lower(%s)",
                    (name,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    return rows[0][0], 0.6

    return None, 0.0


def ocr_match(reader: OcrReader, pool, crop_bgr: np.ndarray) -> MethodResult:
    """Method B: score = mean OCR confidence * DB-resolution exactness."""
    name, number, mean_conf = reader.read_regions(crop_bgr)
    card_ref_id, exactness = resolve(pool, name, number)
    return MethodResult(
        method="b", card_ref_id=card_ref_id, score=mean_conf * exactness
    )
