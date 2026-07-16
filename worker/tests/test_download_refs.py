import json
from pathlib import Path

from scripts.download_refs import card_to_row, needs_download

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
