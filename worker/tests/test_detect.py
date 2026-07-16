from notbulk.detect import detect_cards
from tests.fixtures import synthetic_photo, card_spec

CFG = {
    "crop": {"width": 734, "height": 1024, "webp_quality": 80},
    "detection": {
        "aspect": 0.714,
        "aspect_tolerance": 0.12,
        "min_area_frac": 0.005,
        "max_cards_per_photo": 30,
        "sharpness_min": 45.0,
    },
}

CARD_W, CARD_H = 250, 350          # aspect ~0.714


def test_detect_single_card():
    photo = synthetic_photo([card_spec(600, 500, CARD_W, CARD_H, inner="grad")])
    dets = detect_cards(photo, CFG)
    assert len(dets) == 1
    d = dets[0]
    assert d.crop.shape == (1024, 734, 3)
    assert d.quad.shape == (4, 2)
    assert d.crop_index == 0
    assert d.sharpness > 0


def test_detect_nine_card_grid_orders_row_major():
    specs, centers = [], []
    for row in range(3):
        for col in range(3):
            cx, cy = 250 + col * 400, 250 + row * 450
            specs.append(card_spec(cx, cy, CARD_W, CARD_H, inner="checker"))
            centers.append((cx, cy))
    photo = synthetic_photo(specs, size=(1600, 1400))
    dets = detect_cards(photo, CFG)
    assert len(dets) == 9
    # crop_index is dense 0..8 with no gaps.
    assert sorted(d.crop_index for d in dets) == list(range(9))
    # Row-major: sorting by crop_index yields rows top-to-bottom, cols left-to-right.
    by_index = sorted(dets, key=lambda d: d.crop_index)
    cys = [d.quad[:, 1].mean() for d in by_index]
    # Three ascending row bands of three.
    for band in range(3):
        trio = by_index[band * 3:band * 3 + 3]
        xs = [d.quad[:, 0].mean() for d in trio]
        assert xs == sorted(xs)                       # left-to-right within row
    assert cys[0] < cys[8]                             # first row above last


def test_non_card_aspect_rectangle_rejected():
    # A wide banner (aspect ~2.0) is not card-aspect and must be filtered out.
    photo = synthetic_photo([card_spec(600, 500, 500, 250, inner="grad")])
    dets = detect_cards(photo, CFG)
    assert dets == []


def test_oversized_count_capped():
    cfg = {**CFG, "detection": {**CFG["detection"], "max_cards_per_photo": 4}}
    specs = []
    for row in range(3):
        for col in range(3):
            specs.append(card_spec(200 + col * 380, 200 + row * 440,
                                   CARD_W, CARD_H, inner="rings"))
    photo = synthetic_photo(specs, size=(1600, 1500))
    dets = detect_cards(photo, cfg)
    assert len(dets) == 4                              # capped
    assert sorted(d.crop_index for d in dets) == [0, 1, 2, 3]


def test_rotated_card_detected():
    photo = synthetic_photo([card_spec(600, 500, CARD_W, CARD_H, angle=12.0)])
    dets = detect_cards(photo, CFG)
    assert len(dets) == 1
    assert dets[0].crop.shape == (1024, 734, 3)


def test_gradient_background_grid_all_detected():
    # Uneven lighting: background brightness ramps 40 -> 160 left-to-right.
    # A global (Otsu-only) threshold merges bright background with the cards
    # and drops detections; the adaptive-INV pass must recover them all.
    specs = []
    for row in range(3):
        for col in range(3):
            specs.append(card_spec(250 + col * 400, 250 + row * 450,
                                   CARD_W, CARD_H, inner="checker"))
    photo = synthetic_photo(specs, size=(1600, 1400), bg_ramp=(40, 160))
    dets = detect_cards(photo, CFG)
    assert len(dets) == 9
    assert sorted(d.crop_index for d in dets) == list(range(9))
