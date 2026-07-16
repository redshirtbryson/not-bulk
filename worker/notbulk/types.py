from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class CropHashes:
    full: int          # 64-bit DCT pHash of grayscale normalized crop
    edge: int          # 64-bit DCT pHash of Sobel edge map
    region_art: int    # 64-bit pHash of art box
    region_name: int   # 64-bit pHash of name band
    region_text: int   # 64-bit pHash of bottom text zone


@dataclass
class Detection:
    quad: np.ndarray       # (4,2) float32, source-photo coords, TL/TR/BR/BL order
    crop: np.ndarray       # BGR uint8, exactly 734x1024 (w x h)
    sharpness: float       # resolution-normalized Laplacian variance
    crop_index: int        # stable ordinal within the photo (left-to-right, top-to-bottom)


@dataclass
class MethodResult:
    method: str                # 'h' | 'a' | 'b' | 'c'
    card_ref_id: str | None    # pokemontcg.io id or None
    score: float               # 0.0-1.0 method-level score


@dataclass
class HashMatch:
    card_ref_id: str
    score: float       # 0.0-1.0
    distance: int      # Hamming distance of top hit (full hash)
    margin: int        # distance gap to second-best distinct card
    agreement: int     # how many of the 5 hash types voted for this card (0-5)


@dataclass
class Identification:
    card_ref_id: str | None
    confidence: int            # 0-100 composite
    accepted_stage: str        # 'h' | 'multi' | 'llm' | 'validation' | 'unreadable'
    rotation: int              # 0 | 90 | 180 | 270 (applied correction)
    methods: list[MethodResult] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # top-3 card_ref_ids for validation UI
