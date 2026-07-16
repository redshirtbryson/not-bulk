import numpy as np
import pytest

from notbulk.ocr import OcrReader, resolve, ocr_match, parse_number, clean_name
from tests.fakes import FakePool, FakeOcrEngine


def test_parse_number_standard():
    assert parse_number("Weakness ... 123/198 ... Illus.") == "123/198"


def test_parse_number_promo():
    assert parse_number("PROMO SWSH039 black star") == "SWSH039"


def test_parse_number_prefers_slash_over_promo():
    assert parse_number("SWSH039 25/185") == "25/185"


def test_parse_number_none_when_absent():
    assert parse_number("just some flavor text") is None


def test_clean_name_strips_hp_and_digits():
    assert clean_name("Charizard 170 HP") == "Charizard"
    assert clean_name("Pikachu HP 60") == "Pikachu"


def test_clean_name_preserves_meaningful_digits():
    # Digits are only noise when adjacent to the HP token; names keep theirs.
    assert clean_name("Porygon2") == "Porygon2"
    assert clean_name("Zygarde 10% Forme") == "Zygarde 10% Forme"
    assert clean_name("Zygarde 50% Forme") == "Zygarde 50% Forme"
    assert clean_name("Pikachu 60 HP") == "Pikachu"
    assert clean_name("HP 120 Snorlax") == "Snorlax"


def _line(text, conf):
    # PaddleOCR line = [box, (text, confidence)]
    return [[[0, 0], [1, 0], [1, 1], [0, 1]], (text, conf)]


def test_read_regions_picks_best_name_and_parses_number():
    # First .ocr call = name band; second = bottom third.
    engine = FakeOcrEngine([
        [_line("Charizard 170 HP", 0.97), _line("noise", 0.40)],   # name band
        [_line("weakness fighting", 0.80), _line("25/185", 0.92)],  # bottom third
    ])
    reader = OcrReader(engine=engine)
    crop = np.zeros((1024, 734, 3), np.uint8)
    name, number, mean_conf = reader.read_regions(crop)
    assert name == "Charizard"
    assert number == "25/185"
    # mean over highest-name-conf (0.97) and all bottom-third confs used for number
    assert 0.0 < mean_conf <= 1.0


def test_read_regions_no_number_returns_none_number():
    engine = FakeOcrEngine([
        [_line("Pikachu HP 60", 0.95)],
        [_line("just flavor text", 0.70)],
    ])
    reader = OcrReader(engine=(np_engine := engine))
    name, number, mean_conf = reader.read_regions(np.zeros((1024, 734, 3), np.uint8))
    assert name == "Pikachu"
    assert number is None


def test_read_regions_empty_name_band():
    engine = FakeOcrEngine([[], [_line("100/100", 0.80)]])
    reader = OcrReader(engine=engine)
    name, number, mean_conf = reader.read_regions(np.zeros((1024, 734, 3), np.uint8))
    assert name is None
    assert number == "100/100"


def test_resolve_number_and_printed_total_unique_is_exact():
    # number '25/185' -> number=25, printed_total=185; one row -> exactness 1.0
    pool = FakePool([[("sv4-25", "Charizard")]])
    card_id, exactness = resolve(pool, "Charizard", "25/185")
    assert card_id == "sv4-25"
    assert exactness == 1.0


def test_resolve_number_multiple_rows_narrows_by_name_difflib():
    # two rows share number/total; difflib picks the closest name -> 0.9
    pool = FakePool([[("sv4-25", "Charizard"), ("sv4-99", "Charmander")]])
    card_id, exactness = resolve(pool, "Charizrd", "25/185")  # typo tolerated
    assert card_id == "sv4-25"
    assert exactness == 0.9


def test_resolve_name_only_unique_fallback():
    # No parsable N/M number -> name-only exact lower() match unique -> 0.6
    pool = FakePool([[("sv4-25",)]])
    card_id, exactness = resolve(pool, "Charizard", None)
    assert card_id == "sv4-25"
    assert exactness == 0.6


def test_resolve_returns_none_when_nothing_matches():
    pool = FakePool([[]])
    card_id, exactness = resolve(pool, None, None)
    assert card_id is None
    assert exactness == 0.0


def test_resolve_normalizes_zero_padded_slash_number():
    # OCR '012/198' must match card_refs.number='12', printed_total='198' -> 1.0
    pool = FakePool([[("sv4-12", "Pikachu")]])
    card_id, exactness = resolve(pool, "Pikachu", "012/198")
    assert card_id == "sv4-12"
    assert exactness == 1.0
    # DB queried with the leading zero stripped from both parts
    assert pool.cursor.executed[0][1] == ("12", "198")


def test_resolve_all_zero_slash_part_normalizes_to_zero():
    # '000/198' degenerates to '0', not empty string
    pool = FakePool([[("xx-0", "Missingno")]])
    card_id, exactness = resolve(pool, None, "000/198")
    assert card_id == "xx-0"
    assert pool.cursor.executed[0][1] == ("0", "198")


def test_resolve_promo_number_zero_not_stripped():
    # Promo forms never match N/M, so they skip the slash-normalized query and
    # fall through to the name-only path; their significant zero stays intact.
    assert parse_number("PROMO SWSH039 black star") == "SWSH039"
    pool = FakePool([[("swshp-39",)]])
    card_id, exactness = resolve(pool, "Pikachu", "SWSH039")
    assert card_id == "swshp-39"
    assert exactness == 0.6
    # Only the name query ran; no number query with a stripped promo code.
    assert len(pool.cursor.executed) == 1
    assert pool.cursor.executed[0][1] == ("Pikachu",)


def test_ocr_match_composes_conf_times_exactness():
    engine = FakeOcrEngine([
        [_line("Charizard 170 HP", 1.0)],       # name band, conf 1.0
        [_line("25/185", 1.0)],                  # bottom third, conf 1.0
    ])
    reader = OcrReader(engine=engine)
    pool = FakePool([[("sv4-25", "Charizard")]])  # unique -> exactness 1.0
    crop = np.zeros((1024, 734, 3), np.uint8)
    r = ocr_match(reader, pool, crop)
    assert r.method == "b"
    assert r.card_ref_id == "sv4-25"
    assert abs(r.score - 1.0) < 1e-6  # mean_conf(1.0) * exactness(1.0)


def test_ocr_match_no_resolution_scores_zero():
    engine = FakeOcrEngine([[], []])
    reader = OcrReader(engine=engine)
    pool = FakePool([[]])
    r = ocr_match(reader, pool, np.zeros((1024, 734, 3), np.uint8))
    assert r.method == "b"
    assert r.card_ref_id is None
    assert r.score == 0.0
