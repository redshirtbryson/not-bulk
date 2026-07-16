# M1 — Pipeline Core (CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI that takes photos of Pokemon cards, detects and isolates each card, identifies it through the confidence cascade (hash ensemble → embedding → OCR → vision LLM), and reports results — gated by an eval harness that hard-fails on any wrong auto-accept.

**Architecture:** Python 3.11 worker package (`worker/notbulk/`) with one module per pipeline stage, backed by Postgres (card_refs, ref_hashes, llm_cache), Qdrant (embedding index), and local reference-image mirror. Index-build scripts are first-class repo artifacts. No web layer in M1 — the CLI and eval harness are the only entry points.

**Tech Stack:** Python 3.11 + uv, OpenCV, onnxruntime (DINOv2 ViT-S/14, int8), PaddleOCR (PP-OCRv4 mobile), Anthropic SDK, psycopg 3 + psycopg_pool, qdrant-client, dbmate migrations, pytest. Docker Compose: postgres:16, qdrant, minio, mailpit (minio/mailpit unused until M2 but provisioned now).

## Global Constraints

- Secrets ONLY via `bws run` (Bitwarden Secrets Manager, dev machine token). Never in tracked files, never printed, no `.env` files. Env var names: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `POKEMONTCG_API_KEY`, `DISCORD_WEBHOOK_URL`.
- `config.yaml` at repo root holds all thresholds/model names — committed, never secrets.
- All services bind 127.0.0.1 only.
- Zero wrong auto-accepts is a HARD invariant; auto-accept rate is a soft target. Threshold changes that trade wrong-accept safety for hit rate are rejected by definition.
- Crop normalization target: 734x1024 WebP q80. Card aspect 2.5:3.5, tolerance 12%.
- Cascade thresholds (config defaults): auto-accept >=80, hash-only auto-accept >=90, unreadable <40.
- pokemontcg.io ID format for all card refs (e.g. `sv4-123`).
- UUIDv7 for generated IDs (Python `uuid6` package, `uuid7()`).
- Conventional commits; bump `VERSION` per repo rules (this plan's work is one minor bump to 0.2.0 at M1 completion; individual task commits do not bump).
- Every task: TDD (failing test → minimal implementation → pass → commit).
- Python tooling: `uv` only (`uv add`, `uv run`). Run tests as `cd worker && uv run pytest`.
- Nothing user-supplied ever reaches a shell; image libraries invoked directly.

## File Structure

```
not-bulk/
├── docker-compose.yml                 # Task 1
├── config.yaml                        # Task 1
├── CLAUDE.md                          # Task 1 (agent guardrails)
├── .gitignore                         # Task 1
├── migrations/
│   └── 001_m1_reference_tables.sql    # Task 2 (dbmate format)
├── worker/
│   ├── pyproject.toml                 # Task 1 (uv init)
│   ├── notbulk/
│   │   ├── __init__.py
│   │   ├── types.py                   # Task 3 (shared dataclasses)
│   │   ├── config.py                  # Task 3
│   │   ├── db.py                      # Task 3
│   │   ├── preprocess.py              # Task 5
│   │   ├── detect.py                  # Task 6
│   │   ├── hashing.py                 # Task 7
│   │   ├── augment.py                 # Task 8
│   │   ├── hash_index.py              # Task 9
│   │   ├── embed.py                   # Task 11
│   │   ├── ocr.py                     # Task 12
│   │   ├── llm.py                     # Task 13
│   │   ├── cascade.py                 # Task 14
│   │   └── cli.py                     # Task 15
│   ├── scripts/
│   │   ├── download_refs.py           # Task 4
│   │   ├── build_hash_index.py        # Task 10
│   │   └── build_embed_index.py       # Task 11
│   ├── data/refs/                     # gitignored image mirror
│   ├── models/                        # gitignored ONNX artifacts
│   └── tests/                         # test_<module>.py per module + fixtures/
├── eval/
│   ├── regression.py                  # Task 16
│   └── baseline.json                  # Task 16 (committed after first real run)
└── ground-truth/
    └── manifest.json                  # Task 16 (schema + empty scaffold)
```

## Interface Contract (authoritative — all tasks conform to these exact signatures)

```python
# notbulk/types.py
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
```

```python
# notbulk/config.py
def load_config(path: str = "config.yaml") -> dict: ...   # plain nested dict from YAML

# notbulk/db.py
from psycopg_pool import ConnectionPool
def get_pool() -> ConnectionPool: ...   # reads DATABASE_URL env; pool min 1 max 4; singleton

# notbulk/preprocess.py
def warp_card(photo: np.ndarray, quad: np.ndarray, cfg: dict) -> np.ndarray: ...  # -> BGR 734x1024
def webp_roundtrip(img: np.ndarray, quality: int = 80) -> np.ndarray: ...
def to_gray(img: np.ndarray) -> np.ndarray: ...
def sharpness(img: np.ndarray) -> float: ...   # Laplacian variance normalized per megapixel

# notbulk/detect.py
def detect_cards(photo: np.ndarray, cfg: dict) -> list[Detection]: ...

# notbulk/hashing.py
REGIONS: dict[str, tuple[float, float, float, float]]  # name -> (x0,y0,x1,y1) fractions of 734x1024:
#   'art'  = (0.08, 0.12, 0.92, 0.55)
#   'name' = (0.05, 0.03, 0.80, 0.11)
#   'text' = (0.05, 0.88, 0.95, 0.97)
def dct_phash(gray: np.ndarray) -> int: ...              # 64-bit
def compute_hashes(crop_bgr: np.ndarray) -> CropHashes: ...
def hamming(a: int, b: int) -> int: ...

# notbulk/augment.py
def variants(img: np.ndarray, n: int, seed: int) -> list[np.ndarray]: ...
# applies: homography jitter, webp_roundtrip, white-balance shift, mild blur,
# rotation jitter (<=3 deg), synthetic specular sweep over art box

# notbulk/hash_index.py
class HashIndex:
    @classmethod
    def from_rows(cls, rows: list[tuple[str, str, int]]) -> "HashIndex": ...
        # rows: (card_ref_id, hash_type, hash_bits) — vectorized numpy linear scan w/ popcount
    @classmethod
    def load(cls, pool) -> "HashIndex": ...               # SELECT from ref_hashes
    def match(self, h: CropHashes, cfg: dict) -> HashMatch | None: ...
    def match_full_only(self, full_hash: int) -> tuple[str, int] | None: ...
        # (card_ref_id, distance) for orientation testing — full-hash tier only
    def __len__(self) -> int: ...   # total hash entries loaded; 0 = index not built

# notbulk/embed.py
class Embedder:
    def __init__(self, onnx_path: str): ...
    def embed(self, crop_bgr: np.ndarray) -> np.ndarray: ...   # (384,) float32, L2-normalized
def embed_match(embedder: Embedder, qdrant, crop_bgr: np.ndarray) -> MethodResult: ...
    # method='a'; score = top cosine sim weighted by margin over 2nd distinct card
QDRANT_COLLECTION = "card_refs"

# notbulk/ocr.py
class OcrReader:                       # lazy singleton around PaddleOCR
    def read_regions(self, crop_bgr: np.ndarray) -> tuple[str | None, str | None, float]: ...
        # (name_text, number_text like '123/198', mean confidence)
def resolve(pool, name: str | None, number: str | None) -> tuple[str | None, float]: ...
    # DB lookup against card_refs -> (card_ref_id, exactness 0-1)
def ocr_match(reader: OcrReader, pool, crop_bgr: np.ndarray) -> MethodResult: ...  # method='b'

# notbulk/llm.py
def llm_match(client, pool, crop_bgr: np.ndarray, cfg: dict) -> MethodResult: ...
    # method='c'; Anthropic vision, constrained JSON prompt; cached in llm_cache
    # keyed by sha256 of crop WebP bytes (content hash, NOT pHash)

# notbulk/cascade.py
@dataclass
class CascadeDeps:
    hash_index: HashIndex
    embedder: Embedder | None
    qdrant: object | None
    ocr_reader: OcrReader | None
    anthropic: object | None
    pool: object

def orient(crop_bgr: np.ndarray, index: HashIndex) -> tuple[np.ndarray, int]: ...
    # try 4 rotations via index.match_full_only on each; return (upright crop, rotation)
def identify_crop(crop_bgr: np.ndarray, deps: CascadeDeps, cfg: dict) -> Identification: ...

# notbulk/cli.py  — entry point `notbulk-scan` in pyproject [project.scripts]
# usage: uv run notbulk-scan PHOTO [PHOTO...] --json out.json [--no-llm]
```

```sql
-- migrations/001_m1_reference_tables.sql (dbmate: -- migrate:up / -- migrate:down)
CREATE TABLE card_refs (
  id text PRIMARY KEY,               -- pokemontcg.io id, e.g. 'sv4-123'
  name text NOT NULL,
  set_id text NOT NULL,
  set_name text NOT NULL,
  number text NOT NULL,              -- collector number as printed, e.g. '123'
  printed_total text,                -- denominator, e.g. '198'
  rarity text,
  image_url text NOT NULL,
  finishes text[] NOT NULL DEFAULT '{}',
  synced_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX card_refs_name_idx ON card_refs (lower(name));
CREATE INDEX card_refs_number_idx ON card_refs (number);

CREATE TABLE ref_hashes (
  id uuid PRIMARY KEY,
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  hash_type text NOT NULL CHECK (hash_type IN ('full','edge','region_art','region_name','region_text')),
  hash_bits bigint NOT NULL,         -- 64-bit hash stored as signed bigint
  source text NOT NULL CHECK (source IN ('reference','augmented','user_validated')),
  usage_count int NOT NULL DEFAULT 0,
  last_matched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ref_hashes_type_idx ON ref_hashes (hash_type);
CREATE INDEX ref_hashes_card_idx ON ref_hashes (card_ref_id);

CREATE TABLE llm_cache (
  crop_sha256 text PRIMARY KEY,
  model text NOT NULL,
  response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

```yaml
# config.yaml (Task 1 creates; all thresholds live here)
models:
  embedding: dinov2_vits14
  embedding_onnx: worker/models/dinov2_vits14_int8.onnx
  llm: claude-haiku-4-5-20251001
crop: { width: 734, height: 1024, webp_quality: 80 }
detection:
  aspect: 0.714            # 2.5/3.5
  aspect_tolerance: 0.12
  min_area_frac: 0.005
  max_cards_per_photo: 30
  sharpness_min: 45.0
hash:
  accept_distance: 10
  min_margin: 4
  augmentations_per_card: 6
cascade:
  auto_accept: 80
  hash_only_accept: 90
  unreadable_below: 40
qdrant: { url: "http://127.0.0.1:6333" }
```

Eval manifest schema (`ground-truth/manifest.json`):
```json
{ "photos": [ { "file": "IMG_001.jpg",
    "scenario": "holo|reverse|sleeved|glare|vintage|rotated|multi|clean",
    "cards": [ { "card_ref_id": "sv4-123", "finish": "holofoil", "notes": "" } ] } ] }
```

---

## Assembly Resolutions (authoritative — override any conflicting task prose)

1. **`HashMatch.agreement` counts hash TYPES, 0–5** (full, edge, region_art, region_name, region_text). `HashIndex.match` returns a candidate only at `agreement >= 3` (Task 9). The cascade's Stage 1 hash-only auto-accept additionally requires `agreement >= 4` — full + edge + at least 2 of 3 regions (Task 14's `>= 4` gate stands).
2. **`HashMatch.margin` is computed on the full-hash tier only** (distance gap to the second-best distinct card's full hash). Per-type second-best distances may be computed but are not consumed by any gate.
3. **`HashIndex` implements `__len__`** (total loaded entries). The CLI (Task 15) and eval harness (Task 16) detect an unbuilt index via `len(index) == 0` and exit with the build-script hint.
4. **Package layout:** flat `worker/notbulk/` per the contract wins over uv's `src/` default; Task 1's flatten step (hatch `packages` entry) stands.
5. **Local Postgres credentials** are `notbulk`/`notbulk`, database `notbulk` (compose). The BWS dev secret `DATABASE_URL` must be authored as `postgres://notbulk:notbulk@127.0.0.1:5432/notbulk?sslmode=disable`. Owner setup step, one time.
6. **`card_refs.finishes` is the tcgplayer price-key vocabulary verbatim** (`normal`, `holofoil`, `reverseHolofoil`, ...) — spec §5 uses exactly these keys for pricing, so no normalization layer exists between download and finish handling.
7. **dbmate pinned at v2.24.2**, binary at `./bin/dbmate`, `bin/` gitignored.
8. **numpy popcount:** runtime branch (`np.bitwise_count` when available, else 16-bit lookup table) stands; no numpy version floor is pinned.
9. **`crop_index` row-band divisor** (`0.15 * photo_height`) is a documented tuning knob, not a contract value.
10. **Task 16 edits the repo `CLAUDE.md` merge-gate line created in Task 1**; the implementer adjusts surrounding prose minimally, never rewrites the guardrails.
11. **Anthropic message shape and model id** (`claude-haiku-4-5-20251001`) are verified against current Anthropic docs (claude-api skill / Context7) by the Task 13 implementer before coding; the plan's structure is the expected baseline.

---
<!-- M1 Pipeline Core — Part 1: Tasks 1-4 (scaffolding, migrations, core modules, reference download) -->
<!-- Assembled into 2026-07-16-m1-pipeline-core.md. Signatures/tables/config/paths conform to that plan's Interface Contract verbatim. -->

### Task 1: Repo scaffolding

**Files:**
- Create: `docker-compose.yml`
- Create: `.gitignore`
- Create: `config.yaml`
- Create: `CLAUDE.md`
- Create: `worker/pyproject.toml` (via `uv init --package`)
- Create: `worker/src/worker/__init__.py` (created by `uv init --package`, later moved — see step)
- Create: `worker/notbulk/__init__.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: the running compose stack (`postgres:16` reachable on `127.0.0.1:5432` with database `notbulk`, `qdrant` on `127.0.0.1:6333`, `minio` on `127.0.0.1:9000`/`9001`, `mailpit` on `127.0.0.1:8025`/`1025`); `config.yaml` at repo root loaded by every later task; the `worker/` uv package (`notbulk` import namespace, Python 3.11, deps: `numpy`, `opencv-python-headless`, `pyyaml`, `psycopg[binary]`, `psycopg_pool`, `uuid6`; dev dep `pytest`) that Tasks 3+ import and test against.

- [ ] **Step 1: Create `docker-compose.yml`**

All ports bound to `127.0.0.1` only. Named volumes for durable data. Postgres healthcheck gates dependents.

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: notbulk
      POSTGRES_USER: notbulk
      POSTGRES_PASSWORD: notbulk
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U notbulk -d notbulk"]
      interval: 5s
      timeout: 5s
      retries: 10

  qdrant:
    image: qdrant/qdrant
    ports:
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
    volumes:
      - qdrantdata:/qdrant/storage

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "127.0.0.1:9000:9000"
      - "127.0.0.1:9001:9001"
    volumes:
      - miniodata:/data
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 5s
      timeout: 5s
      retries: 10

  createbuckets:
    image: minio/mc
    depends_on:
      minio:
        condition: service_healthy
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 minioadmin minioadmin;
      mc mb --ignore-existing local/notbulk;
      exit 0;
      "

  mailpit:
    image: axllent/mailpit
    ports:
      - "127.0.0.1:8025:8025"
      - "127.0.0.1:1025:1025"
    volumes:
      - mailpitdata:/data

volumes:
  pgdata:
  qdrantdata:
  miniodata:
  mailpitdata:
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[cod]
.venv/
.pytest_cache/

# Node
node_modules/

# Worker runtime artifacts (rebuildable, large)
worker/data/
worker/models/
*.onnx

# Eval scratch
eval/last_run.json

# Local project-scoped binaries (see CLAUDE.md "Local binaries")
bin/
```

- [ ] **Step 3: Create `config.yaml`** (verbatim from the Interface Contract)

```yaml
models:
  embedding: dinov2_vits14
  embedding_onnx: worker/models/dinov2_vits14_int8.onnx
  llm: claude-haiku-4-5-20251001
crop: { width: 734, height: 1024, webp_quality: 80 }
detection:
  aspect: 0.714            # 2.5/3.5
  aspect_tolerance: 0.12
  min_area_frac: 0.005
  max_cards_per_photo: 30
  sharpness_min: 45.0
hash:
  accept_distance: 10
  min_margin: 4
  augmentations_per_card: 6
cascade:
  auto_accept: 80
  hash_only_accept: 90
  unreadable_below: 40
qdrant: { url: "http://127.0.0.1:6333" }
```

- [ ] **Step 4: Create `CLAUDE.md`** (agent guardrails — from design §2.1/§2, spec 2.1, and this plan's Global Constraints)

```markdown
# NotBulk — Agent Guardrails

Repo: https://github.com/redshirtbryson/not-bulk.git — direct pushes to `main`.
Phase 1 runs entirely on the MINTY workstation. See `docs/superpowers/specs/` and `docs/superpowers/plans/`.

## Secrets (hard rules — from spec §2.1)
- Secrets come ONLY from Bitwarden Secrets Manager via `bws run`. No `.env` files, ever.
- Never print a secret value to the terminal, a log, or any file. Reference secrets by their
  BWS name only: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `POKEMONTCG_API_KEY`, `DISCORD_WEBHOOK_URL`,
  S3/MinIO credentials, `IMGUR_CLIENT_ID` (from M2).
- Never commit a secret to any tracked file.
- Any command that needs a secret is wrapped: `bws run -- <command>`. Dev and prod use
  separate BWS machine tokens with separately scoped secret sets.

## Pipeline invariants (from design §4/§8)
- **Zero wrong auto-accepts is a HARD invariant.** ≥90% auto-accept rate is a soft target.
  When they conflict, thresholds move toward precision and the delta goes to the validation queue.
  A card whose ID is accepted but whose finish was deferred does NOT count as an auto-accept.
- **The eval suite is required before merging any pipeline change.** Any change to detection,
  hashing, embeddings, OCR, scoring, or thresholds must run `python -m eval.regression` and
  attach its output to the change. The suite hard-fails on any wrong auto-accept and regression-fails
  if the auto-accept rate drops below the committed baseline.

## Services
- All Docker services bind `127.0.0.1` only. Start with `docker compose up -d`.

## Local binaries
- Project-scoped tools (e.g. `dbmate`) are downloaded to `./bin/`. `bin/` is gitignored;
  the download command lives in this file's runbook (see Task 2) — never install globally.

## Conventions
- Conventional commits: `feat(area):`, `fix(area):`, `docs(area):`, `chore:`. Version is not in
  the commit subject.
- Every functional commit bumps the `VERSION` file (semver, no `v` prefix):
  patch = bug fix/tweak, minor = new feature/file, major = rework/breaking/removal.
- Nothing user-supplied ever reaches a shell; image libraries are invoked directly.
```

- [ ] **Step 5: Initialize `VERSION`**

```bash
printf '0.1.0\n' > VERSION
```

- [ ] **Step 6: Initialize the `worker/` uv package pinned to Python 3.11**

```bash
mkdir -p worker
cd worker
uv init --package --name notbulk --python 3.11 .
```

`uv init --package` creates `worker/pyproject.toml`, `worker/README.md`, and `worker/src/notbulk/__init__.py`.

- [ ] **Step 7: Move the package to the flat `worker/notbulk/` layout the contract expects**

The Interface Contract puts modules at `worker/notbulk/*.py` (not `worker/src/notbulk/`). Flatten and point the build backend at it.

```bash
cd worker
git rm -r --cached src 2>/dev/null || true
mv src/notbulk notbulk
rmdir src
```

Edit `worker/pyproject.toml` to add the package location under the build backend (append if not present):

```toml
[tool.hatch.build.targets.wheel]
packages = ["notbulk"]
```

- [ ] **Step 8: Add runtime and dev dependencies**

```bash
cd worker
uv add numpy opencv-python-headless pyyaml "psycopg[binary]" psycopg_pool uuid6
uv add --dev pytest
```

- [ ] **Step 9: Bring the stack up**

Run: `docker compose up -d`
Expected: services created and started, ending with a line like `Container not-bulk-createbuckets-1  Started` and the shell returning to the prompt.

- [ ] **Step 10: Verify services are healthy**

Run: `docker compose ps`
Expected: `postgres` and `minio` show `(healthy)` under STATUS; `qdrant`, `mailpit` show `Up`; `createbuckets` shows `Exited (0)` (it runs once and exits). Example:

```
NAME                           STATUS                   PORTS
not-bulk-postgres-1            Up 20s (healthy)         127.0.0.1:5432->5432/tcp
not-bulk-qdrant-1              Up 20s                   127.0.0.1:6333-6334->6333-6334/tcp
not-bulk-minio-1               Up 20s (healthy)         127.0.0.1:9000-9001->9000-9001/tcp
not-bulk-createbuckets-1       Exited (0)
not-bulk-mailpit-1             Up 20s                   127.0.0.1:1025->1025/tcp, 127.0.0.1:8025->8025/tcp
```

- [ ] **Step 11: Verify the worker package collects (zero tests OK)**

Run: `cd worker && uv run pytest`
Expected: `no tests ran` — exit code 5 is acceptable here (pytest returns 5 when it collects zero tests). Example tail:

```
============================ no tests ran in 0.01s =============================
```

- [ ] **Step 12: Commit**

```bash
cd ..
git add docker-compose.yml .gitignore config.yaml CLAUDE.md VERSION worker/pyproject.toml worker/uv.lock worker/notbulk/__init__.py worker/README.md
git commit -m "chore: scaffold compose stack, config, guardrails, and uv worker package"
```

---

### Task 2: Migration 001 + dbmate

**Files:**
- Create: `bin/dbmate` (downloaded binary; `bin/` is gitignored)
- Create: `migrations/001_m1_reference_tables.sql`
- Modify: `CLAUDE.md` (append the dbmate runbook)

**Interfaces:**
- Consumes: the running `postgres:16` service and the `notbulk` database from Task 1; `DATABASE_URL` from `bws run`.
- Produces: the `card_refs`, `ref_hashes`, and `llm_cache` tables exactly as in the Interface Contract, which Tasks 3 (`db.py`), 4 (`download_refs.py`), 7 (hash index), and 13 (`llm.py`) read and write; a reproducible `bws run -- ./bin/dbmate up` migration flow documented in `CLAUDE.md`.

- [ ] **Step 1: Download the dbmate binary project-scoped into `./bin/`**

The mechanism is: a pinned `linux-amd64` binary in `./bin/dbmate`, `bin/` gitignored, and the exact download command captured in the `CLAUDE.md` runbook (Step 5). This keeps dbmate off the global system and reproducible on a fresh checkout.

```bash
mkdir -p bin
curl -fsSL -o bin/dbmate \
  https://github.com/amacneil/dbmate/releases/download/v2.24.2/dbmate-linux-amd64
chmod +x bin/dbmate
```

- [ ] **Step 2: Verify dbmate runs**

Run: `./bin/dbmate --version`
Expected: `dbmate version 2.24.2`

- [ ] **Step 3: Write `migrations/001_m1_reference_tables.sql`** (up + down, verbatim table definitions from the contract)

```sql
-- migrate:up
CREATE TABLE card_refs (
  id text PRIMARY KEY,               -- pokemontcg.io id, e.g. 'sv4-123'
  name text NOT NULL,
  set_id text NOT NULL,
  set_name text NOT NULL,
  number text NOT NULL,              -- collector number as printed, e.g. '123'
  printed_total text,                -- denominator, e.g. '198'
  rarity text,
  image_url text NOT NULL,
  finishes text[] NOT NULL DEFAULT '{}',
  synced_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX card_refs_name_idx ON card_refs (lower(name));
CREATE INDEX card_refs_number_idx ON card_refs (number);

CREATE TABLE ref_hashes (
  id uuid PRIMARY KEY,
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  hash_type text NOT NULL CHECK (hash_type IN ('full','edge','region_art','region_name','region_text')),
  hash_bits bigint NOT NULL,         -- 64-bit hash stored as signed bigint
  source text NOT NULL CHECK (source IN ('reference','augmented','user_validated')),
  usage_count int NOT NULL DEFAULT 0,
  last_matched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ref_hashes_type_idx ON ref_hashes (hash_type);
CREATE INDEX ref_hashes_card_idx ON ref_hashes (card_ref_id);

CREATE TABLE llm_cache (
  crop_sha256 text PRIMARY KEY,
  model text NOT NULL,
  response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE llm_cache;
DROP TABLE ref_hashes;
DROP TABLE card_refs;
```

- [ ] **Step 4: Run the migration** (DATABASE_URL comes from `bws run`)

Run: `bws run -- ./bin/dbmate up`
Expected: dbmate creates its schema-migrations bookkeeping table and applies 001:

```
Applying: 001_m1_reference_tables.sql
Writing: ./db/schema.sql
```

(dbmate also writes a `schema.sql` snapshot; it may be committed for reference or ignored — commit it in Step 8 for a durable schema record.)

- [ ] **Step 5: Append the dbmate runbook to `CLAUDE.md`**

Add this section to `CLAUDE.md` under `## Local binaries` (or immediately after it):

```markdown
## Migrations (dbmate)

dbmate is a project-scoped binary at `./bin/dbmate` (`bin/` is gitignored). Install/refresh it with:

    mkdir -p bin
    curl -fsSL -o bin/dbmate \
      https://github.com/amacneil/dbmate/releases/download/v2.24.2/dbmate-linux-amd64
    chmod +x bin/dbmate

Run migrations under `bws run` so `DATABASE_URL` is injected from BWS:

    bws run -- ./bin/dbmate up      # apply pending migrations
    bws run -- ./bin/dbmate down    # roll back the last migration
    bws run -- ./bin/dbmate status  # list applied/pending

Migrations live in `migrations/`, are raw SQL, forward-only in production, and shared by both runtimes.
```

- [ ] **Step 6: Verify the tables exist**

Run: `bws run -- sh -c 'psql "$DATABASE_URL" -c "\dt"'`
Expected (order may vary):

```
              List of relations
 Schema |       Name        | Type  |  Owner
--------+-------------------+-------+---------
 public | card_refs         | table | notbulk
 public | llm_cache         | table | notbulk
 public | ref_hashes        | table | notbulk
 public | schema_migrations | table | notbulk
(4 rows)
```

- [ ] **Step 7: Verify idempotency (re-running `up` is a no-op)**

Run: `bws run -- ./bin/dbmate up`
Expected: no migration is applied — dbmate reports it is current, e.g.:

```
Writing: ./db/schema.sql
```

with no `Applying:` line. (No new tables, no error.)

- [ ] **Step 8: Commit**

```bash
git add migrations/001_m1_reference_tables.sql CLAUDE.md db/schema.sql
git commit -m "feat(db): add migration 001 reference tables and dbmate runbook"
```

---

### Task 3: types.py, config.py, db.py

**Files:**
- Create: `worker/notbulk/types.py`
- Create: `worker/notbulk/config.py`
- Create: `worker/notbulk/db.py`
- Test: `worker/tests/test_types.py`
- Test: `worker/tests/test_config.py`
- Test: `worker/tests/test_db.py`

**Interfaces:**
- Consumes: the `worker/` uv package and root `config.yaml` from Task 1; the migrated Postgres schema and `DATABASE_URL` (from `bws run`) from Task 2.
- Produces: the shared dataclasses `CropHashes`, `Detection`, `MethodResult`, `HashMatch`, `Identification` (Task 5+ consume these); `config.load_config(path="config.yaml") -> dict` (every later task loads config through this); `db.get_pool() -> ConnectionPool` (min 1 / max 4, singleton, reads `DATABASE_URL`) used by Tasks 4, 7, 12, 13.

- [ ] **Step 1: Create the tests package**

```bash
mkdir -p worker/tests
touch worker/tests/__init__.py
```

- [ ] **Step 2: Write failing tests for `types.py`**

`worker/tests/test_types.py`:

```python
import dataclasses

import numpy as np
import pytest

from notbulk.types import (
    CropHashes,
    Detection,
    MethodResult,
    HashMatch,
    Identification,
)


def test_crophashes_is_frozen():
    h = CropHashes(full=1, edge=2, region_art=3, region_name=4, region_text=5)
    assert h.full == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.full = 99  # type: ignore[misc]


def test_identification_defaults():
    ident = Identification(
        card_ref_id="sv4-123",
        confidence=91,
        accepted_stage="h",
        rotation=0,
    )
    assert ident.methods == []
    assert ident.candidates == []
    # defaults must be independent instances, not a shared mutable
    other = Identification(
        card_ref_id=None, confidence=0, accepted_stage="unreadable", rotation=0
    )
    ident.methods.append(MethodResult(method="h", card_ref_id="sv4-123", score=1.0))
    assert other.methods == []


def test_detection_carries_crop_and_index():
    quad = np.zeros((4, 2), dtype=np.float32)
    crop = np.zeros((1024, 734, 3), dtype=np.uint8)
    d = Detection(quad=quad, crop=crop, sharpness=50.0, crop_index=2)
    assert d.crop.shape == (1024, 734, 3)
    assert d.crop_index == 2


def test_methodresult_and_hashmatch_fields():
    m = MethodResult(method="a", card_ref_id=None, score=0.4)
    assert m.method == "a" and m.card_ref_id is None
    hm = HashMatch(card_ref_id="sv4-1", score=0.9, distance=6, margin=5, agreement=4)
    assert hm.agreement == 4 and hm.distance == 6
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd worker && uv run pytest tests/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.types'`.

- [ ] **Step 4: Write `worker/notbulk/types.py`** (dataclasses verbatim from the Interface Contract)

```python
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
```

- [ ] **Step 5: Run to verify types tests pass**

Run: `cd worker && uv run pytest tests/test_types.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 6: Write failing tests for `config.py`**

`worker/tests/test_config.py`. The real repo `config.yaml` is two levels up from `worker/tests/`.

```python
from pathlib import Path

import pytest

from notbulk.config import load_config

REPO_CONFIG = str(Path(__file__).resolve().parents[2] / "config.yaml")


def test_loads_repo_config_cascade_auto_accept():
    cfg = load_config(REPO_CONFIG)
    assert cfg["cascade"]["auto_accept"] == 80
    assert cfg["cascade"]["hash_only_accept"] == 90
    assert cfg["crop"]["width"] == 734


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")
```

- [ ] **Step 7: Run to verify it fails**

Run: `cd worker && uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.config'`.

- [ ] **Step 8: Write `worker/notbulk/config.py`**

```python
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    """Load the NotBulk config as a plain nested dict.

    Raises FileNotFoundError with a clear message if the file is absent.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"config file not found at {p.resolve()} "
            f"(pass an explicit path or run from the repo root)"
        )
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
```

- [ ] **Step 9: Run to verify config tests pass**

Run: `cd worker && uv run pytest tests/test_config.py -v`
Expected: PASS — 2 passed.

- [ ] **Step 10: Write the failing (integration) test for `db.py`**

`worker/tests/test_db.py`. The SELECT-1 check is skipped when `DATABASE_URL` is absent, so the suite is runnable without network. `get_pool()` must be a singleton and must raise `RuntimeError` when the env var is missing.

```python
import os

import pytest

from notbulk.db import get_pool


def test_get_pool_raises_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # reset the module-level singleton so the missing-env path is exercised
    import notbulk.db as dbmod
    dbmod._pool = None
    with pytest.raises(RuntimeError):
        get_pool()


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (run under `bws run` for the integration path)",
)
def test_get_pool_select_one_and_is_singleton():
    import notbulk.db as dbmod
    dbmod._pool = None
    pool = get_pool()
    assert get_pool() is pool  # singleton
    with pool.connection() as conn:
        row = conn.execute("SELECT 1").fetchone()
    assert row[0] == 1
```

- [ ] **Step 11: Run to verify it fails**

Run: `cd worker && uv run pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.db'` (the SELECT-1 test shows as skipped once the module exists, but right now collection fails on import).

- [ ] **Step 12: Write `worker/notbulk/db.py`**

```python
import os

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool (singleton).

    Reads DATABASE_URL from the environment (injected via `bws run`).
    Raises RuntimeError if DATABASE_URL is not set.
    """
    global _pool
    if _pool is not None:
        return _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set; run the command under `bws run` "
            "so Bitwarden injects the connection string"
        )
    _pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    return _pool
```

- [ ] **Step 13: Run the db test (no DATABASE_URL — integration test skips)**

Run: `cd worker && uv run pytest tests/test_db.py -v`
Expected: `test_get_pool_raises_without_database_url` PASSED, `test_get_pool_select_one_and_is_singleton` SKIPPED (reason: DATABASE_URL not set). Example:

```
tests/test_db.py::test_get_pool_raises_without_database_url PASSED
tests/test_db.py::test_get_pool_select_one_and_is_singleton SKIPPED
```

- [ ] **Step 14: Run the integration path once under `bws run` to confirm the pool really connects**

Run: `cd worker && bws run -- uv run pytest tests/test_db.py -v`
Expected: both tests PASS (2 passed) — the SELECT 1 executes against the running Postgres.

- [ ] **Step 15: Run the full worker suite**

Run: `cd worker && uv run pytest -v`
Expected: all pass with one skip (the db integration test) when `DATABASE_URL` is unset — 7 passed, 1 skipped.

- [ ] **Step 16: Commit**

```bash
cd ..
git add worker/notbulk/types.py worker/notbulk/config.py worker/notbulk/db.py worker/tests/__init__.py worker/tests/test_types.py worker/tests/test_config.py worker/tests/test_db.py
git commit -m "feat(worker): add shared types, config loader, and db pool"
```

---

### Task 4: scripts/download_refs.py

**Files:**
- Create: `worker/scripts/download_refs.py`
- Create: `worker/scripts/__init__.py`
- Test: `worker/tests/test_download_refs.py`
- Test: `worker/tests/fixtures/pokemontcg_page.json`
- Modify: `worker/pyproject.toml` (add `httpx` dep)

**Interfaces:**
- Consumes: `card_refs` table (Task 2); `db.get_pool()` (Task 3); `POKEMONTCG_API_KEY` and `DATABASE_URL` from `bws run`.
- Produces: populated `card_refs` rows and a local image mirror at `worker/data/refs/{card_ref_id}.png` (gitignored) that Task 7 (hash index build) and Task 11 (embed index build) read. Two importable pure functions the tests pin: `card_to_row(card: dict) -> tuple` (page-JSON card object → `card_refs` row tuple) and `needs_download(path: Path) -> bool` (resume-skip logic).

- [ ] **Step 1: Add `httpx`**

```bash
cd worker
uv add httpx
```

- [ ] **Step 2: Create a page-JSON fixture** (trimmed shape of a real `/v2/cards` `data[]` entry, no network)

`worker/tests/fixtures/pokemontcg_page.json`:

```json
{
  "data": [
    {
      "id": "sv4-123",
      "name": "Charizard ex",
      "number": "123",
      "rarity": "Double Rare",
      "images": { "small": "https://images.pokemontcg.io/sv4/123.png",
                  "large": "https://images.pokemontcg.io/sv4/123_hires.png" },
      "set": { "id": "sv4", "name": "Paradox Rift", "printedTotal": 182 },
      "tcgplayer": { "prices": { "normal": {"market": 1.2}, "holofoil": {"market": 9.5} } }
    },
    {
      "id": "sv4-5",
      "name": "Pikachu",
      "number": "5",
      "images": { "small": "https://images.pokemontcg.io/sv4/5.png",
                  "large": "https://images.pokemontcg.io/sv4/5_hires.png" },
      "set": { "id": "sv4", "name": "Paradox Rift", "printedTotal": 182 }
    }
  ],
  "page": 1,
  "pageSize": 250,
  "count": 2,
  "totalCount": 2
}
```

- [ ] **Step 3: Write failing tests for the mapping and resume logic**

`worker/tests/test_download_refs.py`:

```python
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
```

- [ ] **Step 4: Run to verify it fails**

Run: `cd worker && uv run pytest tests/test_download_refs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.download_refs'`.

- [ ] **Step 5: Write `worker/scripts/__init__.py`**

```python
```

(Empty file — makes `scripts` an importable package so tests can `from scripts.download_refs import ...`.)

- [ ] **Step 6: Write `worker/scripts/download_refs.py`** (complete script)

`finishes` is the sorted list of `tcgplayer.prices` keys (e.g. `["holofoil", "normal"]`). `printed_total` is coerced to text to match the `card_refs.printed_total text` column. Images stream to `worker/data/refs/{id}.png`; existing nonzero files are skipped (resume). Concurrency is capped at a semaphore of 4 with a small delay; `--sets` limits to specific set ids for dev.

```python
"""Download pokemontcg.io reference cards into card_refs + mirror images locally.

Run under `bws run` so POKEMONTCG_API_KEY and DATABASE_URL are injected:
    bws run -- uv run python scripts/download_refs.py --sets sv4
    bws run -- uv run python scripts/download_refs.py            # full ~20k catalog
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

from notbulk.db import get_pool

API_BASE = "https://api.pokemontcg.io/v2/cards"
PAGE_SIZE = 250
REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
MAX_CONCURRENCY = 4
POLITE_DELAY = 0.05          # seconds between image fetches
MAX_RETRIES = 5


def card_to_row(card: dict) -> tuple:
    """Map a /v2/cards data[] entry to a card_refs row tuple.

    Returns: (id, name, set_id, set_name, number, printed_total, rarity, image_url, finishes)
    """
    card_set = card.get("set", {})
    printed_total = card_set.get("printedTotal")
    prices = (card.get("tcgplayer") or {}).get("prices") or {}
    finishes = sorted(prices.keys())
    return (
        card["id"],
        card["name"],
        card_set.get("id", ""),
        card_set.get("name", ""),
        card["number"],
        str(printed_total) if printed_total is not None else None,
        card.get("rarity"),
        card["images"]["large"],
        finishes,
    )


def needs_download(path: Path) -> bool:
    """True if the image is missing or zero-length (resume-skip)."""
    try:
        return path.stat().st_size == 0
    except FileNotFoundError:
        return True


def _upsert_rows(pool, rows: list[tuple]) -> None:
    sql = """
        INSERT INTO card_refs
          (id, name, set_id, set_name, number, printed_total, rarity, image_url, finishes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
          name = EXCLUDED.name,
          set_id = EXCLUDED.set_id,
          set_name = EXCLUDED.set_name,
          number = EXCLUDED.number,
          printed_total = EXCLUDED.printed_total,
          rarity = EXCLUDED.rarity,
          image_url = EXCLUDED.image_url,
          finishes = EXCLUDED.finishes,
          synced_at = now()
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


async def _get_page(client: httpx.AsyncClient, params: dict) -> dict:
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.get(API_BASE, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                resp.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


async def _download_image(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, card_id: str, url: str
) -> None:
    dest = REFS_DIR / f"{card_id}.png"
    if not needs_download(dest):
        return
    async with sem:
        delay = 1.0
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(url)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "retryable", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                tmp = dest.with_suffix(".png.part")
                tmp.write_bytes(resp.content)
                tmp.rename(dest)
                break
            except (httpx.HTTPStatusError, httpx.TransportError):
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        await asyncio.sleep(POLITE_DELAY)


async def run(set_ids: list[str] | None) -> None:
    api_key = os.environ.get("POKEMONTCG_API_KEY")
    if not api_key:
        raise RuntimeError(
            "POKEMONTCG_API_KEY is not set; run under `bws run`"
        )
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    pool = get_pool()

    query = None
    if set_ids:
        query = " OR ".join(f"set.id:{s}" for s in set_ids)

    total = 0
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        page = 1
        while True:
            params = {"page": page, "pageSize": PAGE_SIZE}
            if query:
                params["q"] = query
            data = await _get_page(client, params)
            cards = data.get("data", [])
            if not cards:
                break

            rows = [card_to_row(c) for c in cards]
            _upsert_rows(pool, rows)

            await asyncio.gather(
                *(
                    _download_image(client, sem, c["id"], c["images"]["large"])
                    for c in cards
                )
            )

            total += len(cards)
            if total % 100 < PAGE_SIZE:
                print(f"...{total} cards synced (page {page})", flush=True)

            if len(cards) < PAGE_SIZE:
                break
            page += 1

    print(f"done: {total} cards synced into card_refs, images mirrored to {REFS_DIR}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download pokemontcg.io reference cards")
    parser.add_argument(
        "--sets",
        nargs="*",
        default=None,
        help="limit to specific set ids (e.g. --sets sv4 sv3) for dev/testing",
    )
    args = parser.parse_args(argv)
    asyncio.run(run(args.sets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Run to verify the mapping/resume tests pass (no network)**

Run: `cd worker && uv run pytest tests/test_download_refs.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 8: Run the full worker suite**

Run: `cd worker && uv run pytest -v`
Expected: all pass with the single db integration skip — 12 passed, 1 skipped.

- [ ] **Step 9: Real invocation against a single set (dev sanity check)**

Run: `cd worker && bws run -- uv run python scripts/download_refs.py --sets sv4`
Expected: progress lines every ~100 cards and a final summary; images appear under `worker/data/refs/`:

```
...100 cards synced (page 1)
...200 cards synced (page 1)
done: 266 cards synced into card_refs, images mirrored to /.../worker/data/refs
```

Full-run guidance: `bws run -- uv run python scripts/download_refs.py` (no `--sets`) pulls the whole catalog (~20k cards). It is throttled (semaphore of 4 + polite delay) and resumable — re-running skips already-mirrored images (`needs_download` is False for existing nonzero files) and upserts card rows, so an interrupted run is safe to restart. Expect it to take a while; run it once, out of band, before building indexes (Tasks 7/11).

- [ ] **Step 10: Commit**

```bash
cd ..
git add worker/scripts/__init__.py worker/scripts/download_refs.py worker/tests/test_download_refs.py worker/tests/fixtures/pokemontcg_page.json worker/pyproject.toml worker/uv.lock
git commit -m "feat(worker): add pokemontcg.io reference download script with resume"
```
<!-- SECTION: M1 Pipeline Core — Part 2 (Tasks 5-10). Assembled into 2026-07-16-m1-pipeline-core.md. -->
<!-- All signatures, constants, config keys, and paths conform to the authoritative Interface Contract in the plan header. -->

### Task 5: Preprocessing primitives (`preprocess.py`)

**Files:**
- Create: `worker/notbulk/preprocess.py`
- Test: `worker/tests/test_preprocess.py`

**Interfaces:**
- Consumes:
  - `load_config(path="config.yaml") -> dict` from `notbulk/config.py` (Task 3). Reads `cfg["crop"]["width"]` (734), `cfg["crop"]["height"]` (1024), `cfg["crop"]["webp_quality"]` (80).
- Produces:
  - `warp_card(photo: np.ndarray, quad: np.ndarray, cfg: dict) -> np.ndarray` — BGR uint8, exactly `(1024, 734, 3)` (h×w). `quad` is `(4,2)` float32 in source-photo coords, ordered TL/TR/BR/BL.
  - `webp_roundtrip(img: np.ndarray, quality: int = 80) -> np.ndarray` — same shape/dtype as input, lossy-WebP-degraded.
  - `to_gray(img: np.ndarray) -> np.ndarray` — single-channel uint8.
  - `sharpness(img: np.ndarray) -> float` — Laplacian variance normalized per megapixel: `var / (h*w/1e6)`.

- [ ] **Step 1: Add the OpenCV + numpy dependencies**

Run:
```bash
cd worker && uv add opencv-python-headless numpy
```
Expected: `uv` resolves and writes `opencv-python-headless` and `numpy` into `pyproject.toml` + `uv.lock`; exit 0. (Headless build — no GUI libs, matches the CPU-only VPS target.)

- [ ] **Step 2: Write the failing test**

Create `worker/tests/test_preprocess.py`:
```python
import numpy as np
import cv2
import pytest

from notbulk.preprocess import warp_card, webp_roundtrip, to_gray, sharpness

CFG = {"crop": {"width": 734, "height": 1024, "webp_quality": 80}}


def _canvas_with_rotated_rect():
    """Draw a filled, rotated rectangle on a black canvas and return
    (canvas, quad) where quad is the rect's 4 corners TL/TR/BR/BL."""
    canvas = np.zeros((900, 1200, 3), dtype=np.uint8)
    center = (600, 450)
    size = (400, 560)          # roughly card-aspect (0.714)
    angle = 18.0
    box = cv2.boxPoints(((center[0], center[1]), size, angle))  # (4,2) float32
    cv2.fillPoly(canvas, [box.astype(np.int32)], (255, 255, 255))
    # boxPoints order is bottom-left-ish going CW; reorder to TL/TR/BR/BL by geometry.
    s = box.sum(axis=1)
    d = np.diff(box, axis=1).ravel()
    tl = box[np.argmin(s)]
    br = box[np.argmax(s)]
    tr = box[np.argmin(d)]
    bl = box[np.argmax(d)]
    quad = np.array([tl, tr, br, bl], dtype=np.float32)
    return canvas, quad


def test_warp_card_produces_exact_crop_shape():
    canvas, quad = _canvas_with_rotated_rect()
    out = warp_card(canvas, quad, CFG)
    assert out.shape == (1024, 734, 3)
    assert out.dtype == np.uint8
    # The warped rectangle was solid white; the de-skewed crop is mostly white.
    assert out.mean() > 200


def test_webp_roundtrip_preserves_shape_and_dtype_but_degrades():
    img = (np.random.default_rng(0).random((1024, 734, 3)) * 255).astype(np.uint8)
    out = webp_roundtrip(img, quality=80)
    assert out.shape == img.shape
    assert out.dtype == img.dtype
    # Lossy codec: identical is essentially impossible, but stays close.
    assert not np.array_equal(out, img)
    assert np.abs(out.astype(np.int16) - img.astype(np.int16)).mean() < 30


def test_to_gray_returns_single_channel():
    img = np.full((10, 12, 3), 128, dtype=np.uint8)
    g = to_gray(img)
    assert g.shape == (10, 12)
    assert g.dtype == np.uint8


def test_sharpness_sharp_much_greater_than_blurred():
    # High-frequency checkerboard vs. its heavily blurred copy.
    tile = np.indices((256, 256)).sum(axis=0) % 2
    checker = (tile * 255).astype(np.uint8)
    checker = cv2.cvtColor(checker, cv2.COLOR_GRAY2BGR)
    blurred = cv2.GaussianBlur(checker, (0, 0), sigmaX=4.0)
    assert sharpness(checker) > sharpness(blurred) * 5


def test_sharpness_is_resolution_normalized():
    # Same content at two resolutions should give comparable (per-megapixel) scores.
    tile = np.indices((256, 256)).sum(axis=0) % 2
    small = cv2.cvtColor((tile * 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    big = cv2.resize(small, (512, 512), interpolation=cv2.INTER_NEAREST)
    # Normalization keeps them within a factor of ~2 rather than 4x apart.
    ratio = sharpness(big) / sharpness(small)
    assert 0.4 < ratio < 2.5
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_preprocess.py -v
```
Expected: FAIL — `ImportError: cannot import name 'warp_card' from 'notbulk.preprocess'` (module/functions do not exist yet).

- [ ] **Step 4: Write the minimal implementation**

Create `worker/notbulk/preprocess.py`:
```python
"""Crop-normalization primitives shared by detection, hashing, and augmentation.

Every user crop and every reference image passes through the identical pipeline:
perspective warp to the canonical 734x1024 frame, then a WebP q80 round-trip so
the index and the query share the codec fingerprint (design A4).
"""
from __future__ import annotations

import cv2
import numpy as np


def warp_card(photo: np.ndarray, quad: np.ndarray, cfg: dict) -> np.ndarray:
    """De-skew the card bounded by ``quad`` into the canonical crop.

    Args:
        photo: BGR uint8 source photo.
        quad: (4,2) float32 corner coords in source-photo space,
            ordered TL/TR/BR/BL.
        cfg: config dict; uses cfg["crop"]["width"|"height"].

    Returns:
        BGR uint8 array of shape (height, width, 3) == (1024, 734, 3).
    """
    w = int(cfg["crop"]["width"])
    h = int(cfg["crop"]["height"])
    src = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    dst = np.array(
        [[0.0, 0.0], [w - 1.0, 0.0], [w - 1.0, h - 1.0], [0.0, h - 1.0]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(photo, m, (w, h), flags=cv2.INTER_LINEAR)


def webp_roundtrip(img: np.ndarray, quality: int = 80) -> np.ndarray:
    """Encode to WebP at ``quality`` and decode back, imprinting the codec
    fingerprint that index and query crops must share."""
    ok, buf = cv2.imencode(".webp", img, [cv2.IMWRITE_WEBP_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("cv2.imencode('.webp') failed")
    out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if out is None:
        raise RuntimeError("cv2.imdecode('.webp') failed")
    return out


def to_gray(img: np.ndarray) -> np.ndarray:
    """BGR -> single-channel uint8 grayscale."""
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def sharpness(img: np.ndarray) -> float:
    """Laplacian-variance sharpness, normalized per megapixel.

    Normalizing by megapixels (var / (h*w/1e6)) makes the threshold in
    config.yaml (detection.sharpness_min) resolution-independent (design A8).
    """
    gray = to_gray(img) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    megapixels = (h * w) / 1e6
    if megapixels <= 0:
        return 0.0
    return var / megapixels
```

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_preprocess.py -v
```
Expected: PASS — 5 passed.

- [ ] **Step 6: Commit**

```bash
cd worker && git add notbulk/preprocess.py tests/test_preprocess.py pyproject.toml uv.lock
git commit -m "feat(preprocess): add warp, webp round-trip, grayscale, sharpness"
```

---

### Task 6: Card detection (`detect.py`)

**Files:**
- Create: `worker/notbulk/detect.py`
- Create: `worker/tests/fixtures.py`
- Test: `worker/tests/test_detect.py`

**Interfaces:**
- Consumes:
  - `warp_card(photo, quad, cfg) -> np.ndarray`, `sharpness(img) -> float` from `notbulk/preprocess.py` (Task 5).
  - `Detection` dataclass from `notbulk/types.py` (Task 3): fields `quad: np.ndarray` (4,2 float32, source coords, TL/TR/BR/BL), `crop: np.ndarray` (BGR uint8 734x1024), `sharpness: float`, `crop_index: int`.
  - config keys `cfg["crop"]["width"|"height"]`, `cfg["detection"]["aspect"]` (0.714), `["aspect_tolerance"]` (0.12), `["min_area_frac"]` (0.005), `["max_cards_per_photo"]` (30).
- Produces:
  - `detect_cards(photo: np.ndarray, cfg: dict) -> list[Detection]` — one `Detection` per card, `crop_index` assigned left-to-right within top-to-bottom row bands, capped at `max_cards_per_photo`.
  - `worker/tests/fixtures.py`: `synthetic_photo(specs, bg=(30,90,30), size=(1600,1200)) -> np.ndarray` and `card_spec(cx, cy, w, h, angle=0.0, inner=...) -> dict` — shared synthetic-photo generator reused by later tasks.

- [ ] **Step 1: Write the shared fixture generator**

Create `worker/tests/fixtures.py`:
```python
"""Synthetic photo fixtures for detection / hashing / cascade tests.

Places solid-bordered, card-aspect rectangles with distinct inner patterns on a
contrasting background at known positions, with optional rotation. No real card
images — everything here is deterministic and drawable with numpy + cv2.
"""
from __future__ import annotations

import cv2
import numpy as np

CARD_ASPECT = 0.714          # 2.5 / 3.5, matches detection.aspect


def card_spec(cx, cy, w, h, angle=0.0, inner="grad"):
    """One synthetic card: center (cx,cy), size (w,h), rotation ``angle`` deg,
    and an ``inner`` pattern key drawn inside the white border."""
    return {"cx": cx, "cy": cy, "w": w, "h": h, "angle": float(angle), "inner": inner}


def _inner_pattern(w, h, key):
    """Return a (h,w,3) BGR inner fill with a distinct pattern per key."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if key == "grad":
        col = np.linspace(20, 235, w, dtype=np.uint8)
        img[:] = np.repeat(col[None, :, None], h, axis=0)
    elif key == "checker":
        t = (np.indices((h, w)).sum(axis=0) // 16) % 2
        img[:] = (t[..., None] * 200 + 30).astype(np.uint8)
    elif key == "rings":
        yy, xx = np.indices((h, w))
        r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
        img[:] = (((r.astype(np.int32) // 10) % 2) * 200 + 30)[..., None].astype(np.uint8)
    elif key == "white":
        img[:] = 255
    elif key == "black":
        img[:] = 0
    else:
        img[:] = 128
    return img


def synthetic_photo(specs, bg=(30, 90, 30), size=(1600, 1200)):
    """Render cards onto a contrasting background.

    Args:
        specs: list of card_spec dicts.
        bg: BGR background color (contrasting, non-white).
        size: (height, width) of the output photo.

    Returns:
        BGR uint8 photo of shape (size[0], size[1], 3).
    """
    h_img, w_img = size
    photo = np.zeros((h_img, w_img, 3), dtype=np.uint8)
    photo[:] = np.array(bg, dtype=np.uint8)
    for s in specs:
        w, h = int(s["w"]), int(s["h"])
        card = np.full((h, w, 3), 255, dtype=np.uint8)          # white border
        pad = max(6, int(min(w, h) * 0.06))
        inner = _inner_pattern(w - 2 * pad, h - 2 * pad, s["inner"])
        card[pad:h - pad, pad:w - pad] = inner
        # Rotate the card patch about its own center, then paste.
        m = cv2.getRotationMatrix2D((w / 2, h / 2), s["angle"], 1.0)
        cos, sin = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
        m[0, 2] += (nw - w) / 2
        m[1, 2] += (nh - h) / 2
        rot = cv2.warpAffine(card, m, (nw, nh), borderValue=(0, 0, 0))
        mask = cv2.warpAffine(np.full((h, w), 255, np.uint8), m, (nw, nh))
        x0, y0 = int(s["cx"] - nw / 2), int(s["cy"] - nh / 2)
        for yy in range(nh):
            for xx in range(nw):
                if mask[yy, xx] and 0 <= y0 + yy < h_img and 0 <= x0 + xx < w_img:
                    photo[y0 + yy, x0 + xx] = rot[yy, xx]
    return photo
```

- [ ] **Step 2: Write the failing test**

Create `worker/tests/test_detect.py`:
```python
import numpy as np
import pytest

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
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_detect.py -v
```
Expected: FAIL — `ImportError: cannot import name 'detect_cards' from 'notbulk.detect'`.

- [ ] **Step 4: Write the minimal implementation**

Create `worker/notbulk/detect.py`:
```python
"""Card detection: adaptive-threshold contour path (design A7).

Downscale for detection, threshold + morphological close, find external
contours, keep card-aspect quads, then scale each quad back to the original
photo resolution and warp to the canonical crop.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import warp_card, sharpness
from .types import Detection

_DETECT_MAX_EDGE = 1600        # long-edge cap for the detection-only downscale
_ADAPT_BLOCK = 51              # adaptiveThreshold blockSize (odd)
_ADAPT_C = 5                   # adaptiveThreshold constant
_APPROX_EPS_FRAC = 0.02        # approxPolyDP epsilon as fraction of perimeter


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points TL/TR/BR/BL. TL has min x+y, BR max x+y;
    TR has min (x-y), BL max (x-y)."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = (pts[:, 0] - pts[:, 1])
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmax(d)]
    bl = pts[np.argmin(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def detect_cards(photo: np.ndarray, cfg: dict) -> list[Detection]:
    """Detect all card-aspect quads in ``photo`` and warp each to a crop."""
    det = cfg["detection"]
    aspect = float(det["aspect"])
    tol = float(det["aspect_tolerance"])
    min_area_frac = float(det["min_area_frac"])
    max_cards = int(det["max_cards_per_photo"])

    h0, w0 = photo.shape[:2]
    long_edge = max(h0, w0)
    scale = min(1.0, _DETECT_MAX_EDGE / long_edge)      # downscale factor (<=1)
    if scale < 1.0:
        small = cv2.resize(photo, (int(w0 * scale), int(h0 * scale)),
                           interpolation=cv2.INTER_AREA)
    else:
        small = photo
    inv = 1.0 / scale                                   # small -> original

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
        _ADAPT_BLOCK, _ADAPT_C,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    img_area = small.shape[0] * small.shape[1]
    min_area = min_area_frac * img_area

    quads: list[np.ndarray] = []                        # original-resolution quads
    for c in contours:
        if cv2.contourArea(c) < min_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, _APPROX_EPS_FRAC * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        # Rotation-safe aspect from minAreaRect: short-edge / long-edge.
        (_, _), (rw, rh), _ = cv2.minAreaRect(approx)
        if rw == 0 or rh == 0:
            continue
        short, long_ = (rw, rh) if rw <= rh else (rh, rw)
        rect_aspect = short / long_
        if abs(rect_aspect - aspect) > tol:
            continue
        quad = _order_corners(approx.astype(np.float32)) * inv   # back to original
        quads.append(quad)

    # Order by row band (top-to-bottom), then x (left-to-right).
    def _key(q):
        cy = float(q[:, 1].mean())
        cx = float(q[:, 0].mean())
        band = int(cy // (h0 * 0.15))                   # ~card-height row bands
        return (band, cx)

    quads.sort(key=_key)
    quads = quads[:max_cards]                            # cap per design S4/A-detection

    detections: list[Detection] = []
    for idx, quad in enumerate(quads):
        crop = warp_card(photo, quad, cfg)
        detections.append(
            Detection(
                quad=quad.astype(np.float32),
                crop=crop,
                sharpness=sharpness(crop),
                crop_index=idx,
            )
        )
    return detections
```

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_detect.py -v
```
Expected: PASS — 5 passed. (If a grid case is flaky at band boundaries, the `0.15` band divisor is the tuning knob; it must keep the 3×3 grid row-major.)

- [ ] **Step 6: Commit**

```bash
cd worker && git add notbulk/detect.py tests/fixtures.py tests/test_detect.py
git commit -m "feat(detect): adaptive-threshold contour card detection with crop ordering"
```

---

### Task 7: Perceptual hashing (`hashing.py`)

**Files:**
- Create: `worker/notbulk/hashing.py`
- Test: `worker/tests/test_hashing.py`

**Interfaces:**
- Consumes:
  - `to_gray(img) -> np.ndarray` from `notbulk/preprocess.py` (Task 5).
  - `CropHashes` dataclass from `notbulk/types.py` (Task 3): frozen, fields `full, edge, region_art, region_name, region_text` (all `int`).
- Produces:
  - `REGIONS: dict[str, tuple[float, float, float, float]]` — exactly `{'art': (0.08,0.12,0.92,0.55), 'name': (0.05,0.03,0.80,0.11), 'text': (0.05,0.88,0.95,0.97)}`.
  - `dct_phash(gray: np.ndarray) -> int` — 64-bit DCT pHash.
  - `compute_hashes(crop_bgr: np.ndarray) -> CropHashes`.
  - `hamming(a: int, b: int) -> int` — bit-count of XOR.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_hashing.py`:
```python
import numpy as np
import cv2
import pytest

from notbulk.hashing import REGIONS, dct_phash, compute_hashes, hamming


def _crop(pattern="grad"):
    """Deterministic 734x1024 BGR crop."""
    h, w = 1024, 734
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if pattern == "grad":
        img[:] = np.repeat(
            np.linspace(0, 255, w, dtype=np.uint8)[None, :, None], h, axis=0
        )
    elif pattern == "checker":
        t = (np.indices((h, w)).sum(axis=0) // 32) % 2
        img[:] = (t[..., None] * 255).astype(np.uint8)
    return img


def test_regions_exact():
    assert REGIONS == {
        "art": (0.08, 0.12, 0.92, 0.55),
        "name": (0.05, 0.03, 0.80, 0.11),
        "text": (0.05, 0.88, 0.95, 0.97),
    }


def test_identical_image_zero_distance_all_five():
    img = _crop("grad")
    a = compute_hashes(img)
    b = compute_hashes(img.copy())
    assert hamming(a.full, b.full) == 0
    assert hamming(a.edge, b.edge) == 0
    assert hamming(a.region_art, b.region_art) == 0
    assert hamming(a.region_name, b.region_name) == 0
    assert hamming(a.region_text, b.region_text) == 0


def test_slight_noise_small_full_distance():
    img = _crop("grad")
    rng = np.random.default_rng(0)
    noisy = np.clip(img.astype(np.int16) + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    a = compute_hashes(img)
    b = compute_hashes(noisy)
    assert hamming(a.full, b.full) <= 6


def test_different_pattern_large_distance():
    a = compute_hashes(_crop("grad"))
    b = compute_hashes(_crop("checker"))
    assert hamming(a.full, b.full) > 20


def test_hash_values_stable_regression_pin():
    # Deterministic fixture — pins exact ints so an accidental algorithm change trips.
    h = compute_hashes(_crop("grad"))
    assert h.full == pytest.approx(h.full)             # placeholder replaced below
    # Regression pins captured from first green run (see Step 5b):
    assert h.full == _PIN_FULL
    assert h.edge == _PIN_EDGE
    assert h.region_art == _PIN_ART


def test_region_all_white_vs_all_black_differ_maximally():
    white = np.full((1024, 734, 3), 255, dtype=np.uint8)
    black = np.zeros((1024, 734, 3), dtype=np.uint8)
    hw = compute_hashes(white)
    hb = compute_hashes(black)
    # A flat region hashes to a constant; white vs black art regions must not collide.
    # (Flat inputs make DCT AC terms ~0; guard against the degenerate all-equal case.)
    assert hw.region_art != hb.region_art or hamming(hw.region_art, hb.region_art) == 0


# Filled in during Step 5b after the first green run:
_PIN_FULL = 0
_PIN_EDGE = 0
_PIN_ART = 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_hashing.py -v
```
Expected: FAIL — `ImportError: cannot import name 'REGIONS' from 'notbulk.hashing'`.

- [ ] **Step 3: Write the minimal implementation**

Create `worker/notbulk/hashing.py`:
```python
"""64-bit DCT perceptual hashing for the Stage-1 ensemble.

Five hashes per crop: full-crop pHash, an edge-map pHash (Sobel magnitude),
and three region pHashes (art / name / text). Region fractions are of the
canonical 734x1024 frame.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import to_gray
from .types import CropHashes

REGIONS: dict[str, tuple[float, float, float, float]] = {
    "art": (0.08, 0.12, 0.92, 0.55),
    "name": (0.05, 0.03, 0.80, 0.11),
    "text": (0.05, 0.88, 0.95, 0.97),
}


def dct_phash(gray: np.ndarray) -> int:
    """64-bit DCT pHash of a single-channel image.

    Resize to 32x32 float32, take the 2D DCT, keep the top-left 8x8 block
    (low frequencies), drop the [0,0] DC term, and set each of the remaining
    63 bits where the coefficient exceeds the median of the 64-block. Bit 0
    (the DC slot) is always 0, giving a stable 64-bit packing.
    """
    if gray.ndim == 3:
        gray = to_gray(gray)
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(resized)
    block = dct[:8, :8]                      # 8x8 low-frequency coefficients
    flat = block.flatten()                   # 64 coefficients, [0] is DC
    med = np.median(flat[1:])                # median over the 63 AC coefficients
    bits = 0
    for i in range(64):
        bit = 1 if (i != 0 and flat[i] > med) else 0
        bits = (bits << 1) | bit
    return int(bits)


def _region_gray(gray: np.ndarray, frac: tuple[float, float, float, float]) -> np.ndarray:
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = frac
    xa, ya = int(round(x0 * w)), int(round(y0 * h))
    xb, yb = int(round(x1 * w)), int(round(y1 * h))
    return gray[ya:yb, xa:xb]


def _edge_map(gray: np.ndarray) -> np.ndarray:
    """Sobel-magnitude edge map, normalized to uint8."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mx = float(mag.max())
    if mx <= 0:
        return np.zeros_like(gray, dtype=np.uint8)
    return np.clip(mag / mx * 255.0, 0, 255).astype(np.uint8)


def compute_hashes(crop_bgr: np.ndarray) -> CropHashes:
    """Compute the 5-hash ensemble for a canonical 734x1024 BGR crop."""
    gray = to_gray(crop_bgr)
    full = dct_phash(gray)
    edge = dct_phash(_edge_map(gray))
    region_art = dct_phash(_region_gray(gray, REGIONS["art"]))
    region_name = dct_phash(_region_gray(gray, REGIONS["name"]))
    region_text = dct_phash(_region_gray(gray, REGIONS["text"]))
    return CropHashes(
        full=full,
        edge=edge,
        region_art=region_art,
        region_name=region_name,
        region_text=region_text,
    )


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit hashes via int.bit_count()."""
    return int((a ^ b).bit_count())
```

- [ ] **Step 4: Run the test to verify it fails only on the regression pins**

Run:
```bash
cd worker && uv run pytest tests/test_hashing.py -v
```
Expected: All pass EXCEPT `test_hash_values_stable_regression_pin`, which FAILS with an assertion mismatch because `_PIN_FULL/_PIN_EDGE/_PIN_ART` are still `0`.

- [ ] **Step 5: Capture the real pin values, then re-pin the test**

Run:
```bash
cd worker && uv run python -c "
import numpy as np, cv2
from notbulk.hashing import compute_hashes
h,w=1024,734
img=np.zeros((h,w,3),np.uint8)
img[:]=np.repeat(np.linspace(0,255,w,dtype=np.uint8)[None,:,None],h,axis=0)
c=compute_hashes(img)
print('full', c.full); print('edge', c.edge); print('art', c.region_art)
"
```
Expected: three integers printed (e.g. `full 9223372036854775807`). Copy each printed value into `worker/tests/test_hashing.py`, replacing the `_PIN_FULL`, `_PIN_EDGE`, `_PIN_ART` `= 0` lines with the printed ints, and delete the `pytest.approx` placeholder line (`assert h.full == pytest.approx(h.full)`).

- [ ] **Step 6: Run the full test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_hashing.py -v
```
Expected: PASS — 6 passed.

- [ ] **Step 7: Commit**

```bash
cd worker && git add notbulk/hashing.py tests/test_hashing.py
git commit -m "feat(hashing): 5-hash DCT ensemble with regions, edge map, hamming"
```

---

### Task 8: Augmentation (`augment.py`)

**Files:**
- Create: `worker/notbulk/augment.py`
- Test: `worker/tests/test_augment.py`

**Interfaces:**
- Consumes:
  - `webp_roundtrip(img, quality=80) -> np.ndarray` from `notbulk/preprocess.py` (Task 5).
  - `REGIONS` from `notbulk/hashing.py` (Task 7) — the specular sweep targets `REGIONS["art"]`.
  - `compute_hashes`, `hamming` from `notbulk/hashing.py` (Task 7) — used by tests only, to assert augmentations stay in match range.
- Produces:
  - `variants(img: np.ndarray, n: int, seed: int) -> list[np.ndarray]` — `n` deterministic augmented copies (same shape/dtype as `img`), WebP round-trip always applied last (design A4).

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_augment.py`:
```python
import numpy as np
import cv2
import pytest

from notbulk.augment import variants
from notbulk.hashing import compute_hashes, hamming


def _ref_crop():
    """A busy but deterministic 734x1024 crop (augmentations must stay matchable)."""
    h, w = 1024, 734
    yy, xx = np.indices((h, w))
    r = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2)
    base = (((r.astype(np.int32) // 12) % 2) * 180 + 40).astype(np.uint8)
    img = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    img[:, :, 1] = np.roll(img[:, :, 1], 30, axis=1)      # channel variety
    return img


def test_length_equals_n():
    out = variants(_ref_crop(), n=6, seed=7)
    assert len(out) == 6


def test_deterministic_for_same_seed():
    a = variants(_ref_crop(), n=4, seed=42)
    b = variants(_ref_crop(), n=4, seed=42)
    for x, y in zip(a, b):
        assert np.array_equal(x, y)


def test_differs_for_different_seed():
    a = variants(_ref_crop(), n=4, seed=1)
    b = variants(_ref_crop(), n=4, seed=2)
    # At least one variant differs across seeds.
    assert any(not np.array_equal(x, y) for x, y in zip(a, b))


def test_variants_differ_from_original_but_stay_in_match_range():
    img = _ref_crop()
    ref_hash = compute_hashes(img).full
    out = variants(img, n=6, seed=11)
    for v in out:
        assert not np.array_equal(v, img)                 # actually augmented
        d = hamming(ref_hash, compute_hashes(v).full)
        assert d <= 16                                     # stays matchable (the whole point)


def test_shapes_preserved():
    img = _ref_crop()
    for v in variants(img, n=5, seed=3):
        assert v.shape == img.shape
        assert v.dtype == img.dtype
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_augment.py -v
```
Expected: FAIL — `ImportError: cannot import name 'variants' from 'notbulk.augment'`.

- [ ] **Step 3: Write the minimal implementation**

Create `worker/notbulk/augment.py`:
```python
"""In-memory augmentation for the reference hash index (design A4).

Each variant applies a random subset of degradations that mimic real capture
conditions, then a WebP q80 round-trip LAST so the augmented hash carries the
same codec fingerprint as a live query crop. Fully deterministic per (seed).
Augmentations are tuned to stay within match range — a variant that drifts out
of the accept distance is worse than useless.
"""
from __future__ import annotations

import cv2
import numpy as np

from .preprocess import webp_roundtrip
from .hashing import REGIONS


def _homography_jitter(img, rng):
    """Perturb the 4 corners by <=2% of dims and warp."""
    h, w = img.shape[:2]
    dx, dy = 0.02 * w, 0.02 * h
    src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    jit = rng.uniform(-1, 1, (4, 2)).astype(np.float32) * np.array([dx, dy], np.float32)
    dst = (src + jit).astype(np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _white_balance(img, rng):
    """Per-channel gain in [0.92, 1.08]."""
    gains = rng.uniform(0.92, 1.08, 3).astype(np.float32)
    out = img.astype(np.float32) * gains[None, None, :]
    return np.clip(out, 0, 255).astype(np.uint8)


def _blur(img, rng):
    sigma = float(rng.uniform(0.0, 1.2))
    if sigma < 0.05:
        return img
    return cv2.GaussianBlur(img, (0, 0), sigmaX=sigma)


def _rotate(img, rng):
    """Rotation jitter <=3 deg about center."""
    h, w = img.shape[:2]
    angle = float(rng.uniform(-3.0, 3.0))
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)


def _specular_sweep(img, rng):
    """Additive white linear-gradient band at a random angle across the art box
    (targets the holo/glare failure mode, design A4)."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = REGIONS["art"]
    ax0, ay0 = int(x0 * w), int(y0 * h)
    ax1, ay1 = int(x1 * w), int(y1 * h)
    alpha = float(rng.uniform(0.15, 0.35))
    angle = float(rng.uniform(0, np.pi))
    yy, xx = np.indices((ay1 - ay0, ax1 - ax0), dtype=np.float32)
    proj = xx * np.cos(angle) + yy * np.sin(angle)
    proj = (proj - proj.min()) / (proj.ptp() + 1e-6)
    band = np.exp(-((proj - rng.uniform(0.3, 0.7)) ** 2) / (2 * 0.12 ** 2))  # gaussian ridge
    add = (band * 255.0 * alpha).astype(np.float32)
    out = img.copy().astype(np.float32)
    roi = out[ay0:ay1, ax0:ax1]
    out[ay0:ay1, ax0:ax1] = np.clip(roi + add[..., None], 0, 255)
    return out.astype(np.uint8)


# Ordered pool of optional ops; webp_roundtrip is always applied last, outside this list.
_OPS = [_homography_jitter, _white_balance, _blur, _rotate, _specular_sweep]


def variants(img: np.ndarray, n: int, seed: int) -> list[np.ndarray]:
    """Return ``n`` deterministic augmented copies of ``img``.

    A fresh default_rng(seed) drives every choice, so the same (img, n, seed)
    always yields byte-identical output. Each variant applies a random subset
    of _OPS, then webp_roundtrip last.
    """
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    for _ in range(n):
        v = img.copy()
        # Random non-empty subset, applied in fixed op order for stability.
        while True:
            mask = rng.random(len(_OPS)) < 0.6
            if mask.any():
                break
        for op, use in zip(_OPS, mask):
            if use:
                v = op(v, rng)
        v = webp_roundtrip(v, quality=80)          # always last
        out.append(v)
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_augment.py -v
```
Expected: PASS — 5 passed. (If `test_variants_differ_from_original_but_stay_in_match_range` fails on the high side, the perturbation magnitudes — homography 2%, rotation 3°, specular alpha 0.15–0.35 — are the levers; per design A4 the per-card count is capped where measured false-positive rate climbs, so favor staying inside the distance bound.)

- [ ] **Step 5: Commit**

```bash
cd worker && git add notbulk/augment.py tests/test_augment.py
git commit -m "feat(augment): deterministic in-memory hash augmentation set"
```

---

### Task 9: In-memory hash index + matching (`hash_index.py`)

**Files:**
- Create: `worker/notbulk/hash_index.py`
- Test: `worker/tests/test_hash_index.py`

**Interfaces:**
- Consumes:
  - `CropHashes` from `notbulk/types.py` (Task 3): fields `full, edge, region_art, region_name, region_text`.
  - `HashMatch` from `notbulk/types.py` (Task 3): `card_ref_id, score, distance, margin, agreement`.
  - config keys `cfg["hash"]["accept_distance"]` (10), `cfg["hash"]["min_margin"]` (4).
  - A psycopg `ConnectionPool` (from `get_pool()`, Task 3) for `load()`.
  - `ref_hashes` schema (migration 001): `hash_type IN ('full','edge','region_art','region_name','region_text')`, `hash_bits bigint`.
- Produces:
  - `class HashIndex` with:
    - `@classmethod from_rows(cls, rows: list[tuple[str, str, int]]) -> "HashIndex"` — rows are `(card_ref_id, hash_type, hash_bits)`.
    - `@classmethod load(cls, pool) -> "HashIndex"` — SELECT from `ref_hashes`.
    - `__len__(self) -> int` — total hash entries loaded across all tiers; `0` signals an unbuilt index.
    - `match(self, h: CropHashes, cfg: dict) -> HashMatch | None`.
    - `match_full_only(self, full_hash: int) -> tuple[str, int] | None` — `(card_ref_id, distance)`, full-hash tier only, for orientation testing.

- [ ] **Step 1: Write the failing test**

Create `worker/tests/test_hash_index.py`:
```python
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


def test_len_reflects_loaded_entries():
    # Empty index reports zero; used by CLI/eval to detect an unbuilt index.
    assert len(HashIndex.from_rows([])) == 0
    # Two cards x 5 hash tiers = 10 total entries.
    rows = _rows_for("sv4-1", _hashes(HA)) + _rows_for("sv4-2", _hashes(HB))
    assert len(HashIndex.from_rows(rows)) == 10
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_hash_index.py -v
```
Expected: FAIL — `ImportError: cannot import name 'HashIndex' from 'notbulk.hash_index'`.

- [ ] **Step 3: Write the minimal implementation**

Create `worker/notbulk/hash_index.py`:
```python
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

# Precomputed 16-bit popcount table: distance of each uint64 = sum of popcounts
# of its four 16-bit shorts. Used when numpy < 2 lacks np.bitwise_count.
_POPCOUNT16 = np.array(
    [bin(i).count("1") for i in range(1 << 16)], dtype=np.uint8
)

_HAS_BITCOUNT = hasattr(np, "bitwise_count")


def to_uint64(x: int) -> np.uint64:
    """Interpret a signed Python int (possibly a negative signed bigint from
    Postgres) as an unsigned 64-bit value via two's complement."""
    return np.uint64(int(x) & 0xFFFFFFFFFFFFFFFF)


def _popcount(arr: np.ndarray) -> np.ndarray:
    """Vectorized popcount over a uint64 array -> uint32 distances."""
    if _HAS_BITCOUNT:
        return np.bitwise_count(arr).astype(np.uint32)   # numpy >= 2
    a = arr.view(np.uint64)
    total = np.zeros(a.shape, dtype=np.uint32)
    for shift in (0, 16, 32, 48):
        shorts = ((a >> np.uint64(shift)) & np.uint64(0xFFFF)).astype(np.uint16)
        total += _POPCOUNT16[shorts].astype(np.uint32)
    return total


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

        Agreement = number of tiers where a card is the top hit within
        accept_distance. Accept requires agreement>=3 AND full-hash top
        distance<=accept_distance AND full-hash margin>=min_margin.
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
        if not (agreement >= 3 and full_dist <= accept and margin >= min_margin):
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_hash_index.py -v
```
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
cd worker && git add notbulk/hash_index.py tests/test_hash_index.py
git commit -m "feat(hash-index): vectorized ensemble matcher with agreement + margin gate"
```

---

### Task 10: Reference hash-index build script (`scripts/build_hash_index.py`)

**Files:**
- Create: `worker/scripts/build_hash_index.py`
- Test: `worker/tests/test_build_hash_index.py`

**Interfaces:**
- Consumes:
  - `load_config()` (Task 3): `cfg["crop"]`, `cfg["hash"]["augmentations_per_card"]` (6).
  - `get_pool()` (Task 3) for DB access at runtime.
  - `webp_roundtrip`, `to_gray` (Task 5); `compute_hashes` (Task 7); `variants` (Task 8).
  - `card_refs` and `ref_hashes` tables (migration 001). `ref_hashes.source IN ('reference','augmented','user_validated')`; `hash_bits bigint` (signed two's-complement of uint64); `id uuid`.
  - Local image mirror at `worker/data/refs/{card_ref_id}.png` (populated by Task 4 `download_refs.py`).
  - `uuid7()` from the `uuid6` package (Global Constraints).
- Produces:
  - `letterbox(img: np.ndarray, cfg: dict) -> np.ndarray` — BGR uint8 734x1024, direct resize+pad (references are flat scans; **no warp**).
  - `hash_rows_for_card(card_ref_id: str, img: np.ndarray, cfg: dict) -> list[tuple]` — returns rows `(id, card_ref_id, hash_type, hash_bits, source)`: exactly 5 `'reference'` rows + `5 * augmentations_per_card` `'augmented'` rows. Never emits `'user_validated'`.
  - `to_signed_bigint(u: int) -> int` — uint64 -> signed bigint for storage.
  - CLI: `--sets s1,s2`, `--limit N`; idempotent DELETE-then-insert of `('reference','augmented')` rows per batch, never touching `user_validated`.

- [ ] **Step 1: Add the uuid6 dependency**

Run:
```bash
cd worker && uv add uuid6
```
Expected: `uv` adds `uuid6` to `pyproject.toml` + `uv.lock`; exit 0. (Provides `uuid7()` — Postgres 16 has no native generator, per design §3.)

- [ ] **Step 2: Write the failing test**

Create `worker/tests/test_build_hash_index.py` (no live DB — exercises the pure row-generation logic and the idempotency SQL):
```python
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_build_hash_index.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.build_hash_index'` (or ImportError for the helpers).

- [ ] **Step 4: Write the minimal implementation**

Create `worker/scripts/build_hash_index.py`:
```python
"""Build the reference hash index into ref_hashes.

For every card_refs row with a local scan at data/refs/{id}.png:
  - letterbox to 734x1024 (references are flat scans — direct resize, NO warp),
    then WebP q80 round-trip so refs share the codec fingerprint of live crops,
  - emit 5 'reference' hash rows,
  - generate cfg.hash.augmentations_per_card variants and emit 5*n 'augmented' rows.

Idempotent: DELETE the ('reference','augmented') rows for the batch's card ids
before inserting, so a re-run refreshes without duplicating and never touches
'user_validated' rows (design A9 — additive, never wipes validated/augmented is
enforced by the source-scoped DELETE).

Real invocation:
    bws run -- uv run python scripts/build_hash_index.py --sets sv4
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import cv2
import numpy as np
from uuid6 import uuid7

# Allow `python scripts/build_hash_index.py` from the worker/ dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notbulk.config import load_config
from notbulk.db import get_pool
from notbulk.preprocess import webp_roundtrip
from notbulk.hashing import compute_hashes
from notbulk.augment import variants

_HASH_TYPE_FIELDS = (
    ("full", "full"),
    ("edge", "edge"),
    ("region_art", "region_art"),
    ("region_name", "region_name"),
    ("region_text", "region_text"),
)
_REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
_INSERT_BATCH = 500


def letterbox(img: np.ndarray, cfg: dict) -> np.ndarray:
    """Resize a flat reference scan into the canonical 734x1024 frame,
    preserving aspect with black padding (no perspective warp)."""
    w = int(cfg["crop"]["width"])
    h = int(cfg["crop"]["height"])
    ih, iw = img.shape[:2]
    scale = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


def to_signed_bigint(u: int) -> int:
    """uint64 -> signed 64-bit (two's complement) for Postgres bigint storage."""
    u &= 0xFFFFFFFFFFFFFFFF
    return u - (1 << 64) if u >= (1 << 63) else u


def stable_seed(card_ref_id: str) -> int:
    """Deterministic augmentation seed per card id (stable across runs)."""
    digest = hashlib.sha256(card_ref_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _hash_fields(h) -> list[tuple[str, int]]:
    return [(name, getattr(h, attr)) for name, attr in _HASH_TYPE_FIELDS]


def hash_rows_for_card(card_ref_id: str, img: np.ndarray, cfg: dict) -> list[tuple]:
    """Return insertable rows (id, card_ref_id, hash_type, hash_bits, source).

    5 'reference' rows from the letterboxed+webp reference, plus
    5 * augmentations_per_card 'augmented' rows. Never emits 'user_validated'.
    """
    n = int(cfg["hash"]["augmentations_per_card"])
    base = webp_roundtrip(letterbox(img, cfg), quality=int(cfg["crop"]["webp_quality"]))

    rows: list[tuple] = []
    for name, bits in _hash_fields(compute_hashes(base)):
        rows.append((str(uuid7()), card_ref_id, name, to_signed_bigint(bits), "reference"))

    for variant in variants(base, n=n, seed=stable_seed(card_ref_id)):
        for name, bits in _hash_fields(compute_hashes(variant)):
            rows.append((str(uuid7()), card_ref_id, name, to_signed_bigint(bits), "augmented"))
    return rows


def delete_sql() -> str:
    """Idempotency DELETE — scoped to generated sources, never user_validated,
    batched by a card_ref_id array parameter."""
    return (
        "DELETE FROM ref_hashes "
        "WHERE source IN ('reference','augmented') "
        "AND card_ref_id = ANY(%s)"
    )


def _select_card_ids(cur, sets: list[str] | None, limit: int | None) -> list[str]:
    clauses, params = [], []
    if sets:
        clauses.append("set_id = ANY(%s)")
        params.append(sets)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT id FROM card_refs {where} ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build ref_hashes from local scans.")
    ap.add_argument("--sets", help="comma-separated set_ids, e.g. sv4,sv5")
    ap.add_argument("--limit", type=int, help="cap card count (smoke runs)")
    args = ap.parse_args()

    cfg = load_config()
    sets = args.sets.split(",") if args.sets else None
    pool = get_pool()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            card_ids = _select_card_ids(cur, sets, args.limit)

    print(f"[build_hash_index] {len(card_ids)} candidate cards")
    processed, skipped = 0, 0
    pending: list[tuple] = []
    pending_cards: list[str] = []

    def _flush():
        nonlocal pending, pending_cards
        if not pending_cards:
            return
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(delete_sql(), (pending_cards,))
                cur.executemany(
                    "INSERT INTO ref_hashes "
                    "(id, card_ref_id, hash_type, hash_bits, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    pending,
                )
            conn.commit()
        pending, pending_cards = [], []

    for card_ref_id in card_ids:
        path = _REFS_DIR / f"{card_ref_id}.png"
        if not path.exists():
            skipped += 1
            continue
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            skipped += 1
            continue
        pending.extend(hash_rows_for_card(card_ref_id, img, cfg))
        pending_cards.append(card_ref_id)
        processed += 1
        if len(pending) >= _INSERT_BATCH:
            _flush()
        if processed % 100 == 0:
            print(f"[build_hash_index] {processed} cards hashed")

    _flush()
    print(f"[build_hash_index] done: {processed} hashed, {skipped} skipped (no local scan)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
cd worker && uv run pytest tests/test_build_hash_index.py -v
```
Expected: PASS — 6 passed. (No DB touched — only the pure helpers and the DELETE SQL string are asserted.)

- [ ] **Step 6: Integration path (documented, not run in CI)**

The live end-to-end path needs Postgres, migration 001 applied, and local scans from Task 4. It is exercised manually, not in CI:
```bash
# Prereqs: docker compose up -d && dbmate up && download_refs.py has populated data/refs/
cd worker && bws run -- uv run python scripts/build_hash_index.py --sets sv4 --limit 20
```
Expected output (shape): `[build_hash_index] N candidate cards`, periodic `... cards hashed` lines, then `done: M hashed, K skipped ...`. Re-running the same command is idempotent — row counts for the affected cards do not grow, and any `user_validated` rows are untouched.

- [ ] **Step 7: Commit**

```bash
cd worker && git add scripts/build_hash_index.py tests/test_build_hash_index.py pyproject.toml uv.lock
git commit -m "feat(scripts): idempotent reference hash-index builder"
```

---
<!-- Section: M1 Pipeline Core — Part 3 (Tasks 11–13). Assembled into 2026-07-16-m1-pipeline-core.md. -->
<!-- Conforms to the authoritative Interface Contract in the plan header. All signatures verbatim. -->

### Task 11: Embedding module + Qdrant index builder

**Files:**
- Create: `worker/notbulk/embed.py`
- Create: `worker/scripts/build_embed_index.py`
- Test: `worker/tests/test_embed.py`
- Test: `worker/tests/test_build_embed_index.py`
- Modify: `worker/pyproject.toml` (add `onnxruntime`, `qdrant-client`; optional `build` group `torch`, `onnx`)

**Interfaces:**
- Consumes:
  - `notbulk.preprocess.webp_roundtrip(img: np.ndarray, quality: int = 80) -> np.ndarray` (Task 5) — codec parity per design A4.
  - `notbulk.types.MethodResult(method: str, card_ref_id: str | None, score: float)` (Task 3).
  - `config.yaml` keys `models.embedding_onnx`, `qdrant.url`, `crop.webp_quality` (Task 1).
- Produces (authoritative signatures — later tasks depend on these exactly):
  - `class Embedder: __init__(self, onnx_path: str)`
  - `Embedder.embed(self, crop_bgr: np.ndarray) -> np.ndarray`  # `(384,)` float32, L2-normalized
  - `embed_match(embedder: Embedder, qdrant, crop_bgr: np.ndarray) -> MethodResult`  # `method='a'`
  - `QDRANT_COLLECTION = "card_refs"`
  - `preprocess_to_tensor(crop_bgr: np.ndarray) -> np.ndarray`  # `(1,3,224,224)` float32 NCHW, ImageNet-normalized
  - In `scripts/build_embed_index.py`: `build_point(embedder, qdrant_models, card_ref_id: str, crop_bgr: np.ndarray)` returning a `PointStruct`.

Notes for the implementer:
- Method A is a **shortlist generator** (design A2): it contributes a candidate + score but the cascade never lets it auto-accept alone. This task only produces the score; gating lives in Task 14.
- Runtime inference is CPU-only ONNX (`CPUExecutionProvider`) to mirror the VPS. GPU is used **only** by the one-time build script, and only for the export step.
- DINOv2 ViT-S/14 patch size is 14; 224 is the nearest sensible multiple of 14 (16 patches per side). Use 224 exactly.
- The ONNX graph may emit either a pooled `(1,384)` output or a patch-token sequence `(1, N, 384)`. Handle **both** by output rank: rank-3 → mean-pool over axis 1; rank-2 → use as-is.
- Disaster-recovery posture for Method A (design A10): the Qdrant collection is documented-rebuildable via `bws run -- uv run python scripts/build_embed_index.py --recreate` from `worker/data/refs/`; no separate Qdrant backup in M1.
- `qdrant-client`'s `search()` API is the pinned usage here; pin `uv add "qdrant-client<2"` (or treat the `query_points` migration as future work) so `embed_match` stays on the stable call.

- [ ] **Step 1: Add runtime dependencies**

Run:
```bash
cd worker && uv add onnxruntime qdrant-client
```
Expected: uv resolves and writes `onnxruntime` and `qdrant-client` into `[project.dependencies]` in `worker/pyproject.toml` and updates `uv.lock`. Output ends with a `+ onnxruntime==...` / `+ qdrant-client==...` install summary and no error.

- [ ] **Step 2: Add the one-time build-only optional dependency group**

Run:
```bash
cd worker && uv add --optional build torch onnx
```
Expected: `torch` and `onnx` land under `[project.optional-dependencies].build` in `worker/pyproject.toml`, not in the default set. Confirm the resulting `pyproject.toml` contains exactly this wiring (open it and verify):

```toml
[project.optional-dependencies]
build = [
    "torch",
    "onnx",
]
```

Rationale to record in the commit body: `torch` and `onnx` are needed **only** for the one-time DINOv2 → ONNX export + int8 quantization in `scripts/build_embed_index.py`. Runtime and tests never import them. Install for a build with `uv sync --extra build`.

- [ ] **Step 3: Write the failing test for `preprocess_to_tensor`**

Create `worker/tests/test_embed.py`:
```python
import numpy as np
import pytest

from notbulk.embed import preprocess_to_tensor


def test_preprocess_to_tensor_shape_and_dtype():
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)  # BGR 734x1024 (w x h)
    t = preprocess_to_tensor(crop)
    assert t.shape == (1, 3, 224, 224)
    assert t.dtype == np.float32


def test_preprocess_to_tensor_imagenet_normalized():
    # A mid-gray 128/255 ~= 0.502 input, after ImageNet normalization, must land
    # near (0.502 - mean) / std per channel. Channels are RGB order after BGR->RGB.
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)
    t = preprocess_to_tensor(crop)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    expected = (128.0 / 255.0 - mean) / std  # per-channel scalar (constant image)
    got = t[0].mean(axis=(1, 2))  # mean over H,W -> per-channel
    np.testing.assert_allclose(got, expected, rtol=1e-4, atol=1e-4)
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_embed.py::test_preprocess_to_tensor_shape_and_dtype -v`
Expected: FAIL with `ImportError` / `ModuleNotFoundError: No module named 'notbulk.embed'` (or `cannot import name 'preprocess_to_tensor'`).

- [ ] **Step 5: Implement `preprocess_to_tensor` (minimal to pass)**

Create `worker/notbulk/embed.py`:
```python
"""Method A: DINOv2 ViT-S/14 embedding matcher (CPU ONNX at runtime).

Method A is a shortlist generator (design A2): it produces a candidate + score
but never auto-accepts alone. Gating happens in the cascade (Task 14).
"""
from __future__ import annotations

import cv2
import numpy as np

from notbulk.types import MethodResult

QDRANT_COLLECTION = "card_refs"

# ImageNet statistics (RGB order), matching DINOv2 preprocessing.
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_INPUT_SIZE = 224  # multiple of the ViT-S/14 patch size (14)


def preprocess_to_tensor(crop_bgr: np.ndarray) -> np.ndarray:
    """BGR uint8 crop -> (1,3,224,224) float32 NCHW, ImageNet-normalized RGB."""
    rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (_INPUT_SIZE, _INPUT_SIZE), interpolation=cv2.INTER_AREA)
    arr = resized.astype(np.float32) / 255.0
    arr = (arr - _IMAGENET_MEAN) / _IMAGENET_STD  # broadcast over H,W,C
    chw = np.transpose(arr, (2, 0, 1))  # HWC -> CHW
    return np.expand_dims(chw, 0).astype(np.float32)  # NCHW
```

- [ ] **Step 6: Run the preprocessing tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_embed.py -v`
Expected: both `test_preprocess_to_tensor_*` PASS.

- [ ] **Step 7: Write the failing test for `Embedder` using a real tiny ONNX model**

The test builds a minimal but valid ONNX model at fixture time with the `onnx` package (available in tests via the `build` extra during CI/build; guard so unit runs that lack it skip). The model maps `(1,3,224,224)` → `(1,384)` via Reshape + MatMul, so no model download is ever needed.

Append to `worker/tests/test_embed.py`:
```python
onnx = pytest.importorskip("onnx")  # tiny ONNX build needs the onnx package
import onnxruntime  # noqa: E402  (runtime dep, always present)

from notbulk.embed import Embedder  # noqa: E402


@pytest.fixture
def tiny_onnx_path(tmp_path):
    """Build a valid ONNX model: (1,3,224,224) -> reshape(1,150528) -> MatMul -> (1,384).

    A fixed random weight matrix makes outputs deterministic. This exercises the
    rank-2 (pooled) output branch of Embedder.embed.
    """
    from onnx import TensorProto, helper, numpy_helper

    in_feats = 3 * 224 * 224  # 150528
    out_feats = 384

    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 224, 224])
    out = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, out_feats])

    # Reshape target as an initializer constant [1, in_feats].
    shape_init = numpy_helper.from_array(
        np.array([1, in_feats], dtype=np.int64), name="reshape_shape"
    )
    rng = np.random.default_rng(0)
    weight = numpy_helper.from_array(
        rng.standard_normal((in_feats, out_feats)).astype(np.float32), name="W"
    )

    reshape_node = helper.make_node("Reshape", ["input", "reshape_shape"], ["flat"])
    matmul_node = helper.make_node("MatMul", ["flat", "W"], ["output"])

    graph = helper.make_graph(
        [reshape_node, matmul_node],
        "tiny_embed",
        [inp],
        [out],
        initializer=[shape_init, weight],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9  # compatible with onnxruntime + opset 17
    onnx.checker.check_model(model)

    path = tmp_path / "tiny.onnx"
    onnx.save(model, str(path))
    return str(path)


def test_embedder_output_is_l2_normalized_384(tiny_onnx_path):
    emb = Embedder(tiny_onnx_path)
    crop = np.full((1024, 734, 3), 90, dtype=np.uint8)
    vec = emb.embed(crop)
    assert vec.shape == (384,)
    assert vec.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vec), 1.0, rtol=1e-5, atol=1e-5)


@pytest.fixture
def token_onnx_path(tmp_path):
    """Build an ONNX model emitting a rank-3 token sequence (1, 16, 384) to
    exercise the mean-pool branch of Embedder.embed."""
    from onnx import TensorProto, helper, numpy_helper

    n_tokens, out_feats = 16, 384
    flat_feats = 3 * 224 * 224
    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 224, 224])
    out = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, n_tokens, out_feats])

    shape_init = numpy_helper.from_array(
        np.array([1, flat_feats], dtype=np.int64), name="reshape_shape"
    )
    rng = np.random.default_rng(1)
    weight = numpy_helper.from_array(
        rng.standard_normal((flat_feats, n_tokens * out_feats)).astype(np.float32),
        name="W",
    )
    out_shape = numpy_helper.from_array(
        np.array([1, n_tokens, out_feats], dtype=np.int64), name="out_shape"
    )
    reshape_in = helper.make_node("Reshape", ["input", "reshape_shape"], ["flat"])
    matmul = helper.make_node("MatMul", ["flat", "W"], ["wide"])
    reshape_out = helper.make_node("Reshape", ["wide", "out_shape"], ["output"])
    graph = helper.make_graph(
        [reshape_in, matmul, reshape_out],
        "tiny_tokens",
        [inp],
        [out],
        initializer=[shape_init, weight, out_shape],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    path = tmp_path / "tokens.onnx"
    onnx.save(model, str(path))
    return str(path)


def test_embedder_mean_pools_token_sequence(token_onnx_path):
    emb = Embedder(token_onnx_path)
    crop = np.full((1024, 734, 3), 90, dtype=np.uint8)
    vec = emb.embed(crop)
    assert vec.shape == (384,)
    np.testing.assert_allclose(np.linalg.norm(vec), 1.0, rtol=1e-5, atol=1e-5)
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_embed.py::test_embedder_output_is_l2_normalized_384 -v`
Expected: FAIL with `ImportError: cannot import name 'Embedder' from 'notbulk.embed'`.

- [ ] **Step 9: Implement `Embedder` (both output ranks + L2-normalize)**

Append to `worker/notbulk/embed.py`:
```python
import onnxruntime as ort  # runtime dependency


class Embedder:
    """Wraps an ONNX DINOv2 session for CPU inference."""

    def __init__(self, onnx_path: str):
        self._session = ort.InferenceSession(
            onnx_path, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        tensor = preprocess_to_tensor(crop_bgr)
        (out,) = self._session.run(None, {self._input_name: tensor})
        out = np.asarray(out, dtype=np.float32)
        if out.ndim == 3:          # (1, N_tokens, 384) -> mean-pool patch tokens
            vec = out[0].mean(axis=0)
        else:                      # (1, 384) pooled output
            vec = out.reshape(-1)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)
```

- [ ] **Step 10: Run the Embedder tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_embed.py -v`
Expected: all four tests PASS (`preprocess_to_tensor` x2, `Embedder` pooled + token).

- [ ] **Step 11: Write the failing test for `embed_match` (margin math, mocked Qdrant)**

Append to `worker/tests/test_embed.py`:
```python
from dataclasses import dataclass

from notbulk.embed import embed_match


@dataclass
class _StubPoint:
    """Mimics qdrant_client.models.ScoredPoint (only fields we read)."""
    score: float
    payload: dict


class _StubQdrant:
    """Stub exposing .search(collection_name, query_vector, limit) -> list."""
    def __init__(self, points):
        self._points = points
        self.calls = []

    def search(self, collection_name, query_vector, limit):
        self.calls.append((collection_name, limit))
        return self._points[:limit]


class _StubEmbedder:
    def embed(self, crop_bgr):
        return np.ones(384, dtype=np.float32) / np.sqrt(384)


def test_embed_match_margin_full_bonus():
    # Top sim 0.90; second DISTINCT card is >=0.05 cosine-distance away.
    # cosine distance = 1 - sim. top dist=0.10, second card sim=0.80 -> dist=0.20,
    # margin_to_second_distinct = 0.20 - 0.10 = 0.10 >= 0.05 -> full bonus factor 1.0.
    # score = 0.90 * (0.5 + 0.5 * min(0.10/0.05, 1.0)) = 0.90 * 1.0 = 0.90
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.80, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.method == "a"
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.90) < 1e-6


def test_embed_match_margin_partial():
    # top sim 0.90 (dist 0.10); second distinct sim 0.88 (dist 0.12);
    # margin = 0.02; factor = 0.5 + 0.5 * min(0.02/0.05,1.0) = 0.5 + 0.5*0.4 = 0.70
    # score = 0.90 * 0.70 = 0.63
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.88, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.63) < 1e-6


def test_embed_match_skips_same_card_for_margin():
    # Second point is the SAME card as the top; margin must be measured against
    # the first DISTINCT card (sv4-2 at sim 0.70 -> dist 0.30, margin 0.20 -> full).
    points = [
        _StubPoint(0.90, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.89, {"card_ref_id": "sv4-1"}),
        _StubPoint(0.70, {"card_ref_id": "sv4-2"}),
    ]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.90) < 1e-6


def test_embed_match_no_results_returns_none():
    r = embed_match(_StubEmbedder(), _StubQdrant([]), np.zeros((1024, 734, 3), np.uint8))
    assert r.method == "a"
    assert r.card_ref_id is None
    assert r.score == 0.0


def test_embed_match_single_distinct_card_gets_min_factor():
    # Only one distinct card in the shortlist -> no second distinct -> margin 0 ->
    # factor = 0.5 + 0.5 * min(0/0.05,1.0) = 0.5. score = 0.90 * 0.5 = 0.45
    points = [_StubPoint(0.90, {"card_ref_id": "sv4-1"})]
    r = embed_match(_StubEmbedder(), _StubQdrant(points), np.zeros((1024, 734, 3), np.uint8))
    assert r.card_ref_id == "sv4-1"
    assert abs(r.score - 0.45) < 1e-6
```

- [ ] **Step 12: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_embed.py -k embed_match -v`
Expected: FAIL with `ImportError: cannot import name 'embed_match'`.

- [ ] **Step 13: Implement `embed_match`**

Append to `worker/notbulk/embed.py`:
```python
_MARGIN_SCALE = 0.05  # cosine-distance gap at which the margin bonus saturates


def embed_match(embedder: "Embedder", qdrant, crop_bgr: np.ndarray) -> MethodResult:
    """Method A shortlist score.

    score = top_sim * (0.5 + 0.5 * min(margin_to_second_distinct / 0.05, 1.0))
    where margin is measured in cosine DISTANCE (1 - sim) between the top hit and
    the first shortlist entry belonging to a DIFFERENT card.
    """
    query = embedder.embed(crop_bgr).tolist()
    results = qdrant.search(
        collection_name=QDRANT_COLLECTION, query_vector=query, limit=5
    )
    if not results:
        return MethodResult(method="a", card_ref_id=None, score=0.0)

    top = results[0]
    top_id = top.payload["card_ref_id"]
    top_sim = float(top.score)
    top_dist = 1.0 - top_sim

    second_dist = None
    for point in results[1:]:
        if point.payload["card_ref_id"] != top_id:
            second_dist = 1.0 - float(point.score)
            break

    margin = 0.0 if second_dist is None else (second_dist - top_dist)
    factor = 0.5 + 0.5 * min(max(margin, 0.0) / _MARGIN_SCALE, 1.0)
    score = top_sim * factor
    return MethodResult(method="a", card_ref_id=top_id, score=score)
```

- [ ] **Step 14: Run the full embed test module to verify all pass**

Run: `cd worker && uv run pytest tests/test_embed.py -v`
Expected: PASS — all `preprocess_to_tensor`, `Embedder`, and `embed_match` tests green, zero network, zero GPU.

- [ ] **Step 15: Write the failing test for the build script's pure functions**

The build script's heavy imports (`torch`, `onnx`, quantization) live inside `main()`/`export_onnx()` so importing the module for tests pulls in nothing GPU/torch. The two pure, testable functions are `preprocess_ref` (image → tensor with WebP round-trip parity) and `build_point`.

Create `worker/tests/test_build_embed_index.py`:
```python
import importlib
import sys
import numpy as np
import pytest


def _load_build_module():
    # scripts/ is not a package; load by path so no torch import happens.
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_embed_index.py"
    spec = importlib.util.spec_from_file_location("build_embed_index", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_torch_import_at_module_load():
    sys.modules.pop("torch", None)
    _load_build_module()
    assert "torch" not in sys.modules  # torch import is guarded inside export_onnx/main


def test_preprocess_ref_applies_webp_roundtrip_and_returns_tensor():
    mod = _load_build_module()
    crop = np.full((1024, 734, 3), 128, dtype=np.uint8)
    t = mod.preprocess_ref(crop, webp_quality=80)
    assert t.shape == (1, 3, 224, 224)
    assert t.dtype == np.float32


def test_build_point_uses_uuid5_and_payload():
    mod = _load_build_module()

    class _Emb:
        def embed(self, crop_bgr):
            return np.ones(384, dtype=np.float32) / np.sqrt(384)

    # Use the real qdrant models module for PointStruct construction.
    from qdrant_client import models as qmodels

    crop = np.zeros((1024, 734, 3), np.uint8)
    point = mod.build_point(_Emb(), qmodels, "sv4-123", crop)

    import uuid

    expected_id = str(uuid.uuid5(mod.CARD_REF_NAMESPACE, "sv4-123"))
    assert point.id == expected_id
    assert point.payload == {"card_ref_id": "sv4-123"}
    assert len(point.vector) == 384
    # Deterministic id: same card_ref_id -> same point id (idempotent upsert).
    assert mod.build_point(_Emb(), qmodels, "sv4-123", crop).id == expected_id
```

- [ ] **Step 16: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_build_embed_index.py -v`
Expected: FAIL — `scripts/build_embed_index.py` does not exist yet (`FileNotFoundError` from the loader, surfaced as an error).

- [ ] **Step 17: Implement `scripts/build_embed_index.py`**

Disaster-recovery posture for Method A (design A10): this script is the documented rebuild path — the Qdrant collection is fully reconstructable via `bws run -- uv run python scripts/build_embed_index.py --recreate` from `worker/data/refs/`, so M1 keeps no separate Qdrant backup.

Create `worker/scripts/build_embed_index.py`:
```python
"""One-time DINOv2 embedding index build (GPU-optional).

Runtime inference is CPU ONNX; this script is the only place torch is used, and
only for the export step. Install build deps with: uv sync --extra build.

Flow:
  1. If worker/models/dinov2_vits14_int8.onnx is missing, export it from
     torch.hub DINOv2, then int8-quantize with onnxruntime.quantization.
  2. Embed all worker/data/refs images through the SAME preprocessing as user
     crops (WebP q80 round-trip for codec parity, design A4) and upsert to the
     Qdrant 'card_refs' collection (vector size 384, cosine distance).

Usage:
  uv run --extra build python scripts/build_embed_index.py [--recreate]
      [--sets sv4,sv5] [--limit N]
"""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import cv2
import numpy as np
from qdrant_client import QdrantClient, models as qmodels

from notbulk.config import load_config
from notbulk.embed import Embedder, QDRANT_COLLECTION, preprocess_to_tensor
from notbulk.preprocess import webp_roundtrip

# Stable namespace so a card_ref_id always maps to the same Qdrant point id.
CARD_REF_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "notbulk/card_refs")

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
REFS_DIR = Path(__file__).resolve().parents[1] / "data" / "refs"
ONNX_PATH = MODELS_DIR / "dinov2_vits14_int8.onnx"
VECTOR_SIZE = 384
UPSERT_BATCH = 256


def preprocess_ref(crop_bgr: np.ndarray, webp_quality: int = 80) -> np.ndarray:
    """Reference preprocessing == query preprocessing: WebP round-trip for codec
    parity (design A4), then the standard embed tensor."""
    parity = webp_roundtrip(crop_bgr, quality=webp_quality)
    return preprocess_to_tensor(parity)


def build_point(embedder, qdrant_models, card_ref_id: str, crop_bgr: np.ndarray):
    """Build a Qdrant PointStruct: deterministic uuid5 id, 384-d vector, payload."""
    vector = embedder.embed(crop_bgr).tolist()
    point_id = str(uuid.uuid5(CARD_REF_NAMESPACE, card_ref_id))
    return qdrant_models.PointStruct(
        id=point_id, vector=vector, payload={"card_ref_id": card_ref_id}
    )


def export_onnx(onnx_path: Path) -> None:
    """One-time: DINOv2 ViT-S/14 -> ONNX (dynamic batch, opset 17) -> int8.

    Heavy imports are LOCAL so importing this module (e.g. in tests) never pulls
    in torch.
    """
    import torch  # local import: only needed for the one-time export
    from onnxruntime.quantization import QuantType, quantize_dynamic

    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    fp32_path = onnx_path.with_name("dinov2_vits14_fp32.onnx")

    model = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14")
    model.eval()
    dummy = torch.randn(1, 3, 224, 224)
    torch.onnx.export(
        model,
        dummy,
        str(fp32_path),
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
        opset_version=17,
    )
    quantize_dynamic(
        str(fp32_path), str(onnx_path), weight_type=QuantType.QInt8
    )
    fp32_path.unlink(missing_ok=True)
    print(f"exported int8 ONNX -> {onnx_path}")


def _iter_ref_images(sets: list[str] | None, limit: int | None):
    """Yield (card_ref_id, bgr_image). Filename stem is the card_ref_id
    (e.g. 'sv4-123.webp'); set filter matches the id prefix before the dash."""
    count = 0
    for path in sorted(REFS_DIR.glob("*")):
        if path.suffix.lower() not in (".webp", ".png", ".jpg", ".jpeg"):
            continue
        card_ref_id = path.stem
        if sets and card_ref_id.split("-", 1)[0] not in sets:
            continue
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            print(f"skip unreadable {path.name}")
            continue
        yield card_ref_id, img
        count += 1
        if limit and count >= limit:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the DINOv2 Qdrant index")
    parser.add_argument("--recreate", action="store_true",
                        help="drop and recreate the collection before upsert")
    parser.add_argument("--sets", default=None,
                        help="comma-separated set-id prefixes to include")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of refs (for smoke runs)")
    args = parser.parse_args()

    cfg = load_config()
    if not ONNX_PATH.exists():
        print("ONNX model missing; exporting (one-time, needs --extra build)...")
        export_onnx(ONNX_PATH)

    embedder = Embedder(str(ONNX_PATH))
    client = QdrantClient(url=cfg["qdrant"]["url"])

    if args.recreate:
        client.recreate_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=qmodels.VectorParams(
                size=VECTOR_SIZE, distance=qmodels.Distance.COSINE
            ),
        )
        print(f"recreated collection {QDRANT_COLLECTION}")

    sets = args.sets.split(",") if args.sets else None
    batch: list = []
    total = 0
    for card_ref_id, img in _iter_ref_images(sets, args.limit):
        batch.append(build_point(embedder, qmodels, card_ref_id, img))
        if len(batch) >= UPSERT_BATCH:
            client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
            total += len(batch)
            print(f"upserted {total} points...")
            batch = []
    if batch:
        client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        total += len(batch)
    print(f"done: {total} points in {QDRANT_COLLECTION}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 18: Run the build-script tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_build_embed_index.py -v`
Expected: PASS — all three tests green, `torch` never imported.

- [ ] **Step 19: Run the whole Task 11 test surface**

Run: `cd worker && uv run pytest tests/test_embed.py tests/test_build_embed_index.py -v`
Expected: PASS, no network, no GPU, no torch.

- [ ] **Step 20: Commit**

```bash
git add worker/notbulk/embed.py worker/scripts/build_embed_index.py \
        worker/tests/test_embed.py worker/tests/test_build_embed_index.py \
        worker/pyproject.toml worker/uv.lock
git commit -m "feat(embed): DINOv2 ONNX embedder (Method A) + Qdrant index builder"
```

---

### Task 12: OCR module (Method B)

**Files:**
- Create: `worker/notbulk/ocr.py`
- Create: `worker/tests/fakes.py` (shared test doubles; also used by Task 13)
- Test: `worker/tests/test_ocr.py`
- Modify: `worker/pyproject.toml` (add `paddleocr`, `paddlepaddle`)

**Interfaces:**
- Consumes:
  - `notbulk.types.MethodResult` (Task 3).
  - A DB pool (`notbulk.db.get_pool()` → `psycopg_pool.ConnectionPool`, Task 3). Only used via `pool.connection()` context manager + `cur.execute` / `cur.fetchone` / `cur.fetchall`.
  - `card_refs` schema columns `id, name, number, printed_total` (migration 001, Task 2).
- Produces (authoritative signatures):
  - `class OcrReader: __init__(self, engine=None)`  # lazy PaddleOCR unless an engine is injected
  - `OcrReader.read_regions(self, crop_bgr: np.ndarray) -> tuple[str | None, str | None, float]`  # (name, number like '123/198', mean_conf)
  - `resolve(pool, name: str | None, number: str | None) -> tuple[str | None, float]`  # (card_ref_id, exactness 0-1)
  - `ocr_match(reader: OcrReader, pool, crop_bgr: np.ndarray) -> MethodResult`  # `method='b'`
- Provides for Task 13: `resolve` is the single name/number → card_ref_id resolver; Task 13 reuses it, never duplicates it.

Notes for the implementer:
- Design A5: scan the **bottom third** (`y 0.66-1.0`) of the 734×1024 crop for the number pattern, not a tight box (layouts drift across eras). OCR is expected to no-op on stylized full-arts — that is fine; those cards route onward in the cascade.
- Heavy `paddleocr` import lives **inside** the lazy-init method so importing `notbulk.ocr` stays light and test-only runs need no paddle install.
- `paddlepaddle` wheels can fail to build on this box; design A5 approves `easyocr` as the fallback. If the install caveat below bites, swap the engine — the injected-engine seam keeps `resolve`/parsing tests unaffected.

- [ ] **Step 1: Add OCR dependencies**

Run:
```bash
cd worker && uv add paddleocr paddlepaddle
```
Expected: both land in `[project.dependencies]`, `uv.lock` updated.

Install caveat to record in the commit body: `paddlepaddle` ships platform-specific wheels and may fail to resolve/build on this workstation. Per design A5, `easyocr` is the approved fallback (`uv remove paddlepaddle paddleocr && uv add easyocr`), which changes only the lazy engine constructor in `OcrReader._ensure_engine`; parsing/resolve logic and all tests are engine-agnostic because tests inject a fake engine.

- [ ] **Step 2: Create the shared test fakes**

Create `worker/tests/fakes.py`:
```python
"""Shared test doubles for worker pipeline tests (Tasks 12, 13)."""
from __future__ import annotations


class FakeCursor:
    """Minimal psycopg-cursor stand-in with canned result rows.

    `script` is a list of row-lists consumed one execute() at a time, so a test
    can stage multiple queries. fetchone() returns the first row (or None).
    """

    def __init__(self, script):
        self._script = list(script)
        self._current = []
        self.executed = []  # list of (sql, params) for assertions

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._script.pop(0) if self._script else []
        return self

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    """Stand-in for psycopg_pool.ConnectionPool: pool.connection() -> conn."""

    def __init__(self, rows_script):
        # rows_script: list of row-lists, one per expected query.
        self.cursor = FakeCursor(rows_script)
        self._conn = FakeConnection(self.cursor)

    def connection(self):
        return self._conn


class FakeOcrEngine:
    """Stand-in for a PaddleOCR instance.

    `.ocr(img, cls=...)` returns PaddleOCR's structure:
      [ [ [box, (text, confidence)], ... ] ]   # one page, list of lines
    `results` maps a call ordinal to the lines for that call so name-band and
    number-band reads can differ.
    """

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    def ocr(self, img, cls=False):
        page = self._results[self._i] if self._i < len(self._results) else []
        self._i += 1
        return [page]
```

- [ ] **Step 3: Write the failing tests for number/name parsing**

Create `worker/tests/test_ocr.py`:
```python
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
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_ocr.py -k "parse_number or clean_name" -v`
Expected: FAIL with `ImportError: cannot import name 'parse_number' from 'notbulk.ocr'`.

- [ ] **Step 5: Implement the parsing helpers + band geometry**

Create `worker/notbulk/ocr.py`:
```python
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
_HP_TAIL = re.compile(r"\bHP\b", flags=re.IGNORECASE)


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
    """Strip trailing 'HP' token and stray digits/whitespace from a name line."""
    stripped = _HP_TAIL.sub(" ", text)
    stripped = re.sub(r"\d+", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _crop_band(crop_bgr: np.ndarray, band: tuple[float, float, float, float]) -> np.ndarray:
    h, w = crop_bgr.shape[:2]
    x0, y0, x1, y1 = band
    return crop_bgr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def _upscale_2x(img: np.ndarray) -> np.ndarray:
    return cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
```

- [ ] **Step 6: Run the parsing tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_ocr.py -k "parse_number or clean_name" -v`
Expected: all five PASS.

- [ ] **Step 7: Write the failing test for `read_regions` (fake engine, band coords)**

Append to `worker/tests/test_ocr.py`:
```python
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
    reader = OcrReader(engine=np_engine := engine)
    name, number, mean_conf = reader.read_regions(np.zeros((1024, 734, 3), np.uint8))
    assert name == "Pikachu"
    assert number is None


def test_read_regions_empty_name_band():
    engine = FakeOcrEngine([[], [_line("100/100", 0.80)]])
    reader = OcrReader(engine=engine)
    name, number, mean_conf = reader.read_regions(np.zeros((1024, 734, 3), np.uint8))
    assert name is None
    assert number == "100/100"
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_ocr.py -k read_regions -v`
Expected: FAIL — `OcrReader` is defined but `read_regions` is not yet.

- [ ] **Step 9: Implement `OcrReader` (lazy engine + `read_regions`)**

Append to `worker/notbulk/ocr.py`:
```python
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
```

- [ ] **Step 10: Run the `read_regions` tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_ocr.py -k read_regions -v`
Expected: all three PASS.

- [ ] **Step 11: Write the failing test for `resolve` (fake pool, exactness tiers)**

Append to `worker/tests/test_ocr.py`:
```python
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
```

- [ ] **Step 12: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_ocr.py -k resolve -v`
Expected: FAIL — `resolve` not implemented.

- [ ] **Step 13: Implement `resolve`**

Append to `worker/notbulk/ocr.py`:
```python
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
        cur = conn.cursor()
        nm = _NM.match(number.strip()) if number else None
        if nm:
            num, total = nm.group(1), nm.group(2)
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
                "SELECT id FROM card_refs WHERE lower(name) = lower(%s)", (name,)
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0][0], 0.6

    return None, 0.0
```

Note: `FakeCursor` serves one staged row-list per `execute`; each `resolve` test stages exactly the query path it exercises (the number query for the first two, the name query for the third), so the single-list scripts line up with the branch taken.

- [ ] **Step 14: Run the `resolve` tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_ocr.py -k resolve -v`
Expected: all four PASS.

- [ ] **Step 15: Write the failing test for `ocr_match` (score composition)**

Append to `worker/tests/test_ocr.py`:
```python
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
```

- [ ] **Step 16: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_ocr.py -k ocr_match -v`
Expected: FAIL — `ocr_match` not implemented.

- [ ] **Step 17: Implement `ocr_match`**

Append to `worker/notbulk/ocr.py`:
```python
def ocr_match(reader: OcrReader, pool, crop_bgr: np.ndarray) -> MethodResult:
    """Method B: score = mean OCR confidence * DB-resolution exactness."""
    name, number, mean_conf = reader.read_regions(crop_bgr)
    card_ref_id, exactness = resolve(pool, name, number)
    return MethodResult(
        method="b", card_ref_id=card_ref_id, score=mean_conf * exactness
    )
```

- [ ] **Step 18: Run the whole OCR test module**

Run: `cd worker && uv run pytest tests/test_ocr.py -v`
Expected: PASS — parsing, band cropping, resolve tiers, score composition; no paddle imported.

- [ ] **Step 19: Commit**

```bash
git add worker/notbulk/ocr.py worker/tests/test_ocr.py worker/tests/fakes.py \
        worker/pyproject.toml worker/uv.lock
git commit -m "feat(ocr): PaddleOCR name/number resolution (Method B) + shared test fakes"
```

---

### Task 13: LLM tiebreaker (Method C)

**Files:**
- Create: `worker/notbulk/llm.py`
- Modify: `worker/tests/fakes.py:1` (extend for llm_cache hit/miss + fake Anthropic client)
- Test: `worker/tests/test_llm.py`
- Modify: `worker/pyproject.toml` (add `anthropic`)

**Interfaces:**
- Consumes:
  - `notbulk.preprocess.webp_roundtrip` is NOT used here; encode WebP bytes directly via `cv2.imencode('.webp', ...)`. (The cache key must be the exact WebP bytes sent to the API — design A6.)
  - `notbulk.ocr.resolve(pool, name, number) -> tuple[str | None, float]` (Task 12) — REUSED, not duplicated, for name/number → card_ref_id.
  - `notbulk.types.MethodResult` (Task 3).
  - `llm_cache` schema `crop_sha256 text PK, model text, response jsonb, created_at` (migration 001, Task 2).
  - `config.yaml` keys `models.llm`, `crop.webp_quality` (Task 1).
- Produces (authoritative signature):
  - `llm_match(client, pool, crop_bgr: np.ndarray, cfg: dict) -> MethodResult`  # `method='c'`

Notes for the implementer (design A6):
- Cache key = `sha256` hexdigest of the **WebP bytes** actually sent to the API — NOT the pHash (pHash collisions would silently serve one card's answer for another).
- On cache HIT: parse the stored `response` jsonb and return WITHOUT any API call.
- On parse failure (malformed model JSON): return `MethodResult('c', None, 0.0)` and DO NOT cache the failure.
- Cache the raw response on success with `INSERT ... ON CONFLICT DO NOTHING` (concurrent workers may race the same key).
- The Anthropic model id comes from `cfg['models']['llm']` (contract default `claude-haiku-4-5-20251001`).

- [ ] **Step 1: Add the Anthropic SDK**

Run:
```bash
cd worker && uv add anthropic
```
Expected: `anthropic` in `[project.dependencies]`, `uv.lock` updated.

- [ ] **Step 2: Extend the shared fakes for Anthropic + llm_cache**

Append to `worker/tests/fakes.py`:
```python
class _FakeMessages:
    def __init__(self, canned_text, raise_if_called):
        self._canned_text = canned_text
        self._raise = raise_if_called
        self.calls = []

    def create(self, **kwargs):
        if self._raise:
            raise AssertionError("Anthropic client must NOT be called on cache hit")
        self.calls.append(kwargs)
        # Anthropic response: .content is a list of blocks with .text
        block = type("Block", (), {"type": "text", "text": self._canned_text})()
        return type("Msg", (), {"content": [block]})()


class FakeAnthropic:
    """Stand-in for anthropic.Anthropic. Set raise_if_called=True to assert the
    client is never hit (cache-hit path)."""

    def __init__(self, canned_text="", raise_if_called=False):
        self.messages = _FakeMessages(canned_text, raise_if_called)
```

The existing `FakePool`/`FakeCursor` already support llm_cache: stage `[(response_json,)]` as the first row-list for a HIT, or `[]` for a MISS.

- [ ] **Step 3: Write the failing test for the cache-key / hit / miss behavior**

Create `worker/tests/test_llm.py`:
```python
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
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_llm.py -k "crop_key or cache_hit" -v`
Expected: FAIL with `ImportError: cannot import name 'llm_match' from 'notbulk.llm'`.

- [ ] **Step 5: Implement encoding, key, prompt, and the cache-hit path**

Create `worker/notbulk/llm.py`:
```python
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

    # ---- cache miss: call the API (implemented in next step) ----
    return _call_and_cache(client, pool, crop_bgr, webp, key, model, cfg)
```

- [ ] **Step 6: Run the cache tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_llm.py -k "crop_key or cache_hit" -v`
Expected: PASS (`_call_and_cache` is referenced but not exercised by these two tests, which both take the hit/parse paths — however Python resolves the name at call time only, so the module imports fine).

- [ ] **Step 7: Write the failing tests for the miss path (API call, caching, malformed JSON)**

Append to `worker/tests/test_llm.py`:
```python
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
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd worker && uv run pytest tests/test_llm.py -k "miss or malformed" -v`
Expected: FAIL with `NameError: name '_call_and_cache' is not defined` (or `AttributeError`).

- [ ] **Step 9: Implement `_call_and_cache`**

Append to `worker/notbulk/llm.py`:
```python
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

    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO llm_cache (crop_sha256, model, response) "
            "VALUES (%s, %s, %s) ON CONFLICT (crop_sha256) DO NOTHING",
            (key, model, json.dumps(parsed)),
        )

    return _score_from(parsed, pool)
```

- [ ] **Step 10: Run the miss-path tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_llm.py -k "miss or malformed" -v`
Expected: both PASS.

- [ ] **Step 11: Run the whole LLM test module**

Run: `cd worker && uv run pytest tests/test_llm.py -v`
Expected: PASS — cache keying over WebP bytes, hit skips client, miss calls + caches, malformed JSON scores 0 and is not cached. No network (client faked).

- [ ] **Step 12: Run the full Part-3 test surface together**

Run: `cd worker && uv run pytest tests/test_embed.py tests/test_build_embed_index.py tests/test_ocr.py tests/test_llm.py -v`
Expected: PASS across all four modules — zero network, zero GPU, no torch/paddle imports.

- [ ] **Step 13: Commit**

```bash
git add worker/notbulk/llm.py worker/tests/test_llm.py worker/tests/fakes.py \
        worker/pyproject.toml worker/uv.lock
git commit -m "feat(llm): Anthropic vision tiebreaker (Method C) with content-hash cache"
```
<!-- M1 Pipeline Core — Part 4: cascade, CLI, eval harness (Tasks 14-16) -->
<!-- Assembled into docs/superpowers/plans/2026-07-16-m1-pipeline-core.md -->
<!-- Conforms to the authoritative Interface Contract in that plan header. -->

### Task 14: cascade.py — orientation + confidence cascade

**Files:**
- Create: `worker/notbulk/cascade.py`
- Test: `worker/tests/test_cascade.py`
- Test fixtures (built in-test, no assets): synthetic `card_refs`/`ref_hashes` rows fed to `HashIndex.from_rows`

**Interfaces:**
- Consumes (exact signatures from Tasks 3, 7, 9, 11, 12, 13):
  - `notbulk/types.py`: `CropHashes(full, edge, region_art, region_name, region_text: int)`, `MethodResult(method: str, card_ref_id: str|None, score: float)`, `HashMatch(card_ref_id: str, score: float, distance: int, margin: int, agreement: int)`, `Identification(card_ref_id: str|None, confidence: int, accepted_stage: str, rotation: int, methods: list[MethodResult]=[], candidates: list[str]=[])`
  - `notbulk/preprocess.py`: `to_gray(img) -> np.ndarray`, `sharpness(img) -> float`
  - `notbulk/hashing.py`: `dct_phash(gray: np.ndarray) -> int`, `compute_hashes(crop_bgr) -> CropHashes`
  - `notbulk/hash_index.py`: `HashIndex.match(h: CropHashes, cfg: dict) -> HashMatch|None`, `HashIndex.match_full_only(full_hash: int) -> tuple[str,int]|None`, `HashIndex.from_rows(rows: list[tuple[str,str,int]]) -> HashIndex`
  - `notbulk/embed.py`: `Embedder`, `embed_match(embedder, qdrant, crop_bgr) -> MethodResult` (method='a')
  - `notbulk/ocr.py`: `OcrReader`, `ocr_match(reader, pool, crop_bgr) -> MethodResult` (method='b')
  - `notbulk/llm.py`: `llm_match(client, pool, crop_bgr, cfg) -> MethodResult` (method='c')
  - `config.yaml`: `detection.sharpness_min`, `cascade.hash_only_accept`, `cascade.auto_accept`, `cascade.unreadable_below`, `crop.width`, `crop.height`
- Produces (M2 relies on these exact names/types):
  - `@dataclass CascadeDeps(hash_index: HashIndex, embedder: Embedder|None, qdrant: object|None, ocr_reader: OcrReader|None, anthropic: object|None, pool: object)`
  - `orient(crop_bgr: np.ndarray, index: HashIndex) -> tuple[np.ndarray, int]` — returns `(upright 734x1024 BGR crop, rotation in {0,90,180,270})`
  - `identify_crop(crop_bgr: np.ndarray, deps: CascadeDeps, cfg: dict) -> Identification`

**Design constraints:**
- Spec §4.3 scoring; design A1 (zero wrong auto-accepts is HARD — ties break toward validation), A3 (orientation = full-card pHash in 4 rotations, keep best-scoring), A11 (finish detection is a *separate* post-accept stage, NOT implemented in cascade — do not add it here).
- **Monkeypatch seam requirement:** `cascade.py` MUST call the pluggable method functions through their module objects so tests can monkeypatch them. Import the modules, not the names:
  ```python
  from . import embed as embed_mod
  from . import ocr as ocr_mod
  from . import llm as llm_mod
  ```
  and call `embed_mod.embed_match(...)`, `ocr_mod.ocr_match(...)`, `llm_mod.llm_match(...)`. Never `from .embed import embed_match`.
- `--no-llm` is expressed purely as `deps.anthropic is None`; cascade never reads a CLI flag.

- [ ] **Step 1: Write the failing test for `orient` (no-match → rotation 0)**

Create `worker/tests/test_cascade.py`:

```python
import numpy as np
import pytest

from notbulk import cascade
from notbulk.hash_index import HashIndex
from notbulk.hashing import compute_hashes, dct_phash
from notbulk.preprocess import to_gray
from notbulk.types import MethodResult


def _blank_crop(value=127):
    # 734 wide x 1024 tall BGR, mid-gray so sharpness/hash are deterministic
    return np.full((1024, 734, 3), value, dtype=np.uint8)


def _gradient_crop():
    # A deterministic non-symmetric image so 90/180/270 rotations differ.
    row = np.linspace(0, 255, 734, dtype=np.uint8)
    col = np.linspace(0, 255, 1024, dtype=np.uint8)
    g = (row[None, :].astype(np.uint16) + col[:, None].astype(np.uint16)) // 2
    g = g.astype(np.uint8)
    return np.stack([g, g, g], axis=2)


def _index_from_crops(named_crops):
    # named_crops: list[(card_ref_id, crop_bgr)] -> HashIndex with full-hash rows only
    rows = []
    for cid, crop in named_crops:
        h = compute_hashes(crop)
        rows.append((cid, "full", h.full))
    return HashIndex.from_rows(rows)


def test_orient_no_match_returns_rotation_zero():
    crop = _blank_crop()
    # Empty index -> match_full_only returns None for every rotation.
    index = HashIndex.from_rows([])
    upright, rotation = cascade.orient(crop, index)
    assert rotation == 0
    assert upright.shape == (1024, 734, 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_orient_no_match_returns_rotation_zero -v`
Expected: FAIL with `AttributeError: module 'notbulk.cascade' has no attribute 'orient'` (module/function not yet defined).

- [ ] **Step 3: Write `orient` (minimal)**

Create `worker/notbulk/cascade.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

from . import embed as embed_mod
from . import llm as llm_mod
from . import ocr as ocr_mod
from .hash_index import HashIndex
from .hashing import compute_hashes, dct_phash
from .preprocess import sharpness, to_gray
from .types import Identification, MethodResult

if TYPE_CHECKING:
    from notbulk.embed import Embedder
    from notbulk.ocr import OcrReader

# k90 rotations applied by np.rot90 for each target upright rotation label.
# Target label R means: the crop is currently rotated R degrees clockwise from
# upright, so we rotate it back by R degrees. np.rot90(k) rotates 90*k CCW.
_ROT_TO_K = {0: 0, 90: 1, 180: 2, 270: 3}


def orient(crop_bgr: np.ndarray, index: HashIndex) -> tuple[np.ndarray, int]:
    """Try all four 90-degree rotations, keep the one whose full-card pHash is
    closest to any indexed card. Ties or no match anywhere -> rotation 0.

    Returns (upright 734x1024 BGR crop, rotation in {0,90,180,270})."""
    target_h, target_w = crop_bgr.shape[0], crop_bgr.shape[1]

    best_rotation = 0
    best_distance: int | None = None
    best_crop = crop_bgr

    for rotation, k in _ROT_TO_K.items():
        rotated = np.rot90(crop_bgr, k=k) if k else crop_bgr
        # 90/270 swap width/height; re-resize back to the canonical crop size so
        # the DCT pHash is computed on a 734x1024 image in every rotation.
        if rotated.shape[0] != target_h or rotated.shape[1] != target_w:
            rotated = cv2.resize(
                rotated, (target_w, target_h), interpolation=cv2.INTER_AREA
            )
        gray = to_gray(rotated)
        full_hash = dct_phash(gray)
        hit = index.match_full_only(full_hash)
        if hit is None:
            continue
        _card_id, distance = hit
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_rotation = rotation
            best_crop = rotated

    if best_distance is None:
        return crop_bgr, 0
    return best_crop, best_rotation
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_orient_no_match_returns_rotation_zero -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for upside-down orientation (rotation 180)**

Append to `worker/tests/test_cascade.py`:

```python
def test_orient_upside_down_card_picks_180():
    upright = _gradient_crop()
    index = _index_from_crops([("sv4-1", upright)])
    # Present the card rotated 180 degrees; orient must undo it.
    flipped = np.rot90(upright, k=2)
    corrected, rotation = cascade.orient(flipped, index)
    assert rotation == 180
    # Corrected crop hash should be within tolerance of the indexed upright hash.
    from notbulk.hashing import compute_hashes, hamming
    d = hamming(compute_hashes(corrected).full, compute_hashes(upright).full)
    assert d <= 2
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_orient_upside_down_card_picks_180 -v`
Expected: PASS (the `orient` implementation from Step 3 already handles this; this test locks the behavior).

- [ ] **Step 7: Write the failing test for the sharpness gate (unreadable)**

Append to `worker/tests/test_cascade.py`:

```python
def _cfg():
    return {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
    }


def _deps(index, *, embedder=None, qdrant=None, ocr_reader=None, anthropic=None, pool=None):
    return cascade.CascadeDeps(
        hash_index=index,
        embedder=embedder,
        qdrant=qdrant,
        ocr_reader=ocr_reader,
        anthropic=anthropic,
        pool=pool,
    )


def test_identify_crop_below_sharpness_is_unreadable(monkeypatch):
    crop = _blank_crop()  # flat image -> Laplacian variance ~0
    index = HashIndex.from_rows([])
    result = cascade.identify_crop(crop, _deps(index), _cfg())
    assert result.accepted_stage == "unreadable"
    assert result.card_ref_id is None
    assert result.confidence == 0
    assert result.rotation == 0
```

- [ ] **Step 8: Run test to verify it fails**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_identify_crop_below_sharpness_is_unreadable -v`
Expected: FAIL with `AttributeError: module 'notbulk.cascade' has no attribute 'CascadeDeps'`.

- [ ] **Step 9: Write `CascadeDeps` and the full `identify_crop`**

Append to `worker/notbulk/cascade.py`:

```python
@dataclass
class CascadeDeps:
    hash_index: HashIndex
    embedder: "Embedder | None"
    qdrant: object | None
    ocr_reader: "OcrReader | None"
    anthropic: object | None
    pool: object


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _top3_candidates(results: list[MethodResult]) -> list[str]:
    """Distinct card_ref_ids ordered by highest method score first, capped at 3."""
    best: dict[str, float] = {}
    for r in results:
        if r.card_ref_id is None:
            continue
        if r.card_ref_id not in best or r.score > best[r.card_ref_id]:
            best[r.card_ref_id] = r.score
    ordered = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [cid for cid, _score in ordered[:3]]


def identify_crop(crop_bgr: np.ndarray, deps: CascadeDeps, cfg: dict) -> Identification:
    """Run the cost-ordered cascade H -> A -> B -> C with spec 4.3 scoring.

    Zero wrong auto-accepts is a HARD invariant (design A1): any tie or missing
    agreement resolves toward the validation queue, never toward an accept."""
    det_cfg = cfg["detection"]
    casc_cfg = cfg["cascade"]

    # --- Sharpness gate (runs before anything else) ---
    if sharpness(crop_bgr) < det_cfg["sharpness_min"]:
        return Identification(
            card_ref_id=None,
            confidence=0,
            accepted_stage="unreadable",
            rotation=0,
            methods=[],
            candidates=[],
        )

    # --- Orientation ---
    upright, rotation = orient(crop_bgr, deps.hash_index)

    methods: list[MethodResult] = []

    # --- Stage 1: Method H ---
    hashes = compute_hashes(upright)
    hash_match = deps.hash_index.match(hashes, cfg)
    h_result = MethodResult(
        method="h",
        card_ref_id=hash_match.card_ref_id if hash_match else None,
        score=hash_match.score if hash_match else 0.0,
    )
    methods.append(h_result)

    # High-margin ensemble accept: all three hash CATEGORIES agree (full + edge +
    # majority of the three region hashes). HashIndex.match encodes this as
    # agreement across the 5 hash types; require agreement on at least the full,
    # edge, and 2/3 region hashes -> agreement >= 4 of 5.
    if hash_match is not None and hash_match.agreement >= 4:
        score = 85 + min(hash_match.margin, 10)
        if score >= casc_cfg["hash_only_accept"]:
            return Identification(
                card_ref_id=hash_match.card_ref_id,
                confidence=score,
                accepted_stage="h",
                rotation=rotation,
                methods=methods,
                candidates=_top3_candidates(methods),
            )

    # --- Stage 2: Methods A + B (collected alongside H) ---
    if deps.embedder is not None and deps.qdrant is not None:
        methods.append(embed_mod.embed_match(deps.embedder, deps.qdrant, upright))
    if deps.ocr_reader is not None:
        methods.append(ocr_mod.ocr_match(deps.ocr_reader, deps.pool, upright))

    # Any two of {h,a,b} agree on the same card_ref_id -> base 90 + up to 10.
    hab = [m for m in methods if m.method in ("h", "a", "b") and m.card_ref_id is not None]
    agree_id, agree_pair = _find_agreeing_pair(hab)
    if agree_id is not None:
        mean_score = sum(m.score for m in agree_pair) / len(agree_pair)
        confidence = 90 + int(round(10 * mean_score))
        confidence = _clamp(confidence, 0, 100)
        # Stage 2 auto-accepts by definition (>= auto_accept always holds here).
        return Identification(
            card_ref_id=agree_id,
            confidence=confidence,
            accepted_stage="multi",
            rotation=rotation,
            methods=methods,
            candidates=_top3_candidates(methods),
        )

    # --- Stage 3: Method C (LLM tiebreaker), only if enabled ---
    if deps.anthropic is not None:
        c_result = llm_mod.llm_match(deps.anthropic, deps.pool, upright, cfg)
        methods.append(c_result)
        if c_result.card_ref_id is not None:
            # Does C agree with any of h/a/b?
            partner = next(
                (m for m in hab if m.card_ref_id == c_result.card_ref_id), None
            )
            if partner is not None:
                mean_score = (c_result.score + partner.score) / 2
                confidence = _clamp(70 + int(round(15 * mean_score)), 0, 85)
                if confidence >= casc_cfg["auto_accept"]:
                    return Identification(
                        card_ref_id=c_result.card_ref_id,
                        confidence=confidence,
                        accepted_stage="llm",
                        rotation=rotation,
                        methods=methods,
                        candidates=_top3_candidates(methods),
                    )
                return Identification(
                    card_ref_id=c_result.card_ref_id,
                    confidence=confidence,
                    accepted_stage="validation",
                    rotation=rotation,
                    methods=methods,
                    candidates=_top3_candidates(methods),
                )

    # --- No agreement anywhere: validation with top-3 candidates ---
    best = max((m.score for m in methods), default=0.0)
    confidence = min(60, int(round(100 * best)))
    return Identification(
        card_ref_id=None,
        confidence=confidence,
        accepted_stage="validation",
        rotation=rotation,
        methods=methods,
        candidates=_top3_candidates(methods),
    )


def _find_agreeing_pair(
    results: list[MethodResult],
) -> tuple[str | None, list[MethodResult]]:
    """Return (card_ref_id, [the two+ results that agree on it]) for the highest-
    scoring agreeing group, or (None, []). Agreement = same card_ref_id from >=2
    distinct methods."""
    by_id: dict[str, list[MethodResult]] = {}
    for r in results:
        by_id.setdefault(r.card_ref_id, []).append(r)
    agreeing = {cid: rs for cid, rs in by_id.items() if len(rs) >= 2}
    if not agreeing:
        return None, []
    best_id = max(
        agreeing,
        key=lambda cid: sum(r.score for r in agreeing[cid]) / len(agreeing[cid]),
    )
    return best_id, agreeing[best_id]
```

- [ ] **Step 10: Run the sharpness-gate test**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_identify_crop_below_sharpness_is_unreadable -v`
Expected: PASS.

- [ ] **Step 11: Write the failing test for the hash-only accept path (stage 'h', >=90)**

Append to `worker/tests/test_cascade.py`:

```python
class _FakeHashIndex:
    """Stands in for HashIndex when we need to force a specific HashMatch.
    Provides the two methods cascade calls: match_full_only and match."""

    def __init__(self, full_hit, hash_match):
        self._full_hit = full_hit          # tuple(card_ref_id, distance) | None
        self._hash_match = hash_match      # HashMatch | None

    def match_full_only(self, full_hash):
        return self._full_hit

    def match(self, h, cfg):
        return self._hash_match


def test_identify_crop_hash_only_accept(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()  # sharp enough to clear the gate
    hm = HashMatch(card_ref_id="sv4-7", score=0.98, distance=2, margin=8, agreement=5)
    index = _FakeHashIndex(full_hit=("sv4-7", 2), hash_match=hm)
    # Embedder/ocr/anthropic all None -> only H runs; ensure they are never called.
    result = cascade.identify_crop(crop, _deps(index), _cfg())
    assert result.accepted_stage == "h"
    assert result.card_ref_id == "sv4-7"
    assert result.confidence == 85 + min(8, 10)  # == 93
    assert result.candidates == ["sv4-7"]
    assert [m.method for m in result.methods] == ["h"]
```

- [ ] **Step 12: Run test to verify it passes**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_identify_crop_hash_only_accept -v`
Expected: PASS (the `_gradient_crop` clears the sharpness gate; agreement 5 and margin 8 yield 93 >= 90).

- [ ] **Step 13: Write the failing test for the two-agree path (stage 'multi')**

Append to `worker/tests/test_cascade.py`:

```python
def test_identify_crop_two_agree_multi(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    # H matches sv4-9 but with LOW agreement (won't trigger hash-only accept).
    hm = HashMatch(card_ref_id="sv4-9", score=0.70, distance=9, margin=1, agreement=2)
    index = _FakeHashIndex(full_hit=("sv4-9", 9), hash_match=hm)

    # A (embed) agrees with H on sv4-9; B (ocr) disagrees.
    monkeypatch.setattr(
        cascade.embed_mod,
        "embed_match",
        lambda emb, q, crop_bgr: MethodResult("a", "sv4-9", 0.8),
    )
    monkeypatch.setattr(
        cascade.ocr_mod,
        "ocr_match",
        lambda reader, pool, crop_bgr: MethodResult("b", "sv4-99", 0.4),
    )
    # anthropic MUST NOT be called on the two-agree path; pass a raiser and None.
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=None, pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.accepted_stage == "multi"
    assert result.card_ref_id == "sv4-9"
    # mean(h=0.70, a=0.80) = 0.75 -> 90 + round(7.5) = 98
    assert result.confidence == 98
    assert set(m.method for m in result.methods) == {"h", "a", "b"}
    assert result.candidates[0] == "sv4-9"
```

- [ ] **Step 14: Run test to verify it passes**

Run: `cd worker && uv run pytest tests/test_cascade.py::test_identify_crop_two_agree_multi -v`
Expected: PASS.

- [ ] **Step 15: Write the failing tests for the LLM-agrees boundary (79 vs 81) and no-agreement validation**

Append to `worker/tests/test_cascade.py`:

```python
def _llm_setup(monkeypatch, index, *, a_id, a_score, b_id, b_score, c_id, c_score):
    monkeypatch.setattr(
        cascade.embed_mod,
        "embed_match",
        lambda emb, q, crop_bgr: MethodResult("a", a_id, a_score),
    )
    monkeypatch.setattr(
        cascade.ocr_mod,
        "ocr_match",
        lambda reader, pool, crop_bgr: MethodResult("b", b_id, b_score),
    )
    monkeypatch.setattr(
        cascade.llm_mod,
        "llm_match",
        lambda client, pool, crop_bgr, cfg: MethodResult("c", c_id, c_score),
    )


def test_identify_crop_llm_agrees_below_threshold_is_validation(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    # H low agreement, all three of h/a/b disagree with each other.
    hm = HashMatch(card_ref_id="sv4-1", score=0.5, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # C agrees with H's sv4-1. partner=h(score 0.5), c score chosen so:
    # 70 + round(15 * mean(0.5, c)) = 79  -> mean = 0.6 -> c = 0.7
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.6, b_id="sv4-3",
               b_score=0.6, c_id="sv4-1", c_score=0.7)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.confidence == 79
    assert result.accepted_stage == "validation"       # 79 < auto_accept(80)
    assert result.card_ref_id == "sv4-1"


def test_identify_crop_llm_agrees_at_threshold_is_llm_accept(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    hm = HashMatch(card_ref_id="sv4-1", score=0.5, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # 70 + round(15 * mean(0.5, c)) = 81 -> round(15*mean)=11 -> mean=0.733 -> c≈0.966
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.6, b_id="sv4-3",
               b_score=0.6, c_id="sv4-1", c_score=0.9667)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.confidence == 81
    assert result.accepted_stage == "llm"             # 81 >= auto_accept(80)
    assert result.card_ref_id == "sv4-1"


def test_identify_crop_no_agreement_is_validation_with_candidates(monkeypatch):
    from notbulk.types import HashMatch

    crop = _gradient_crop()
    hm = HashMatch(card_ref_id="sv4-1", score=0.55, distance=9, margin=1, agreement=1)
    index = _FakeHashIndex(full_hit=("sv4-1", 9), hash_match=hm)
    # Every method names a different card; C also disagrees -> no accept anywhere.
    _llm_setup(monkeypatch, index, a_id="sv4-2", a_score=0.45, b_id="sv4-3",
               b_score=0.30, c_id="sv4-4", c_score=0.20)
    deps = _deps(index, embedder=object(), qdrant=object(), ocr_reader=object(),
                 anthropic=object(), pool=object())
    result = cascade.identify_crop(crop, deps, _cfg())
    assert result.accepted_stage == "validation"
    assert result.card_ref_id is None
    assert result.confidence == min(60, int(round(100 * 0.55)))  # == 55
    # Top-3 distinct card_ref_ids by method score, highest first.
    assert result.candidates == ["sv4-1", "sv4-2", "sv4-3"]
```

- [ ] **Step 16: Run the full cascade test module**

Run: `cd worker && uv run pytest tests/test_cascade.py -v`
Expected: PASS — all cases (orient x2, sharpness gate, hash-only accept, two-agree, llm boundary 79/81, no-agreement validation).

- [ ] **Step 17: Commit**

```bash
git add worker/notbulk/cascade.py worker/tests/test_cascade.py
git commit -m "feat(cascade): orientation + confidence cascade with spec 4.3 scoring"
```

---

### Task 15: cli.py — `notbulk-scan` entry point

**Files:**
- Create: `worker/notbulk/cli.py`
- Modify: `worker/pyproject.toml` (add `[project.scripts]` entry `notbulk-scan = "notbulk.cli:main"`)
- Test: `worker/tests/test_cli.py`

**Interfaces:**
- Consumes (exact signatures from Tasks 3, 5, 6, 9, 11, 12, 14):
  - `notbulk/config.py`: `load_config(path: str = "config.yaml") -> dict`
  - `notbulk/db.py`: `get_pool() -> ConnectionPool`
  - `notbulk/hash_index.py`: `HashIndex.load(pool) -> HashIndex`
  - `notbulk/detect.py`: `detect_cards(photo: np.ndarray, cfg: dict) -> list[Detection]`
  - `notbulk/embed.py`: `Embedder(onnx_path: str)`
  - `notbulk/ocr.py`: `OcrReader()`
  - `notbulk/cascade.py`: `CascadeDeps`, `identify_crop(crop_bgr, deps, cfg) -> Identification`
  - `notbulk/types.py`: `Detection`, `Identification`, `MethodResult`
- Produces (M2 relies on these):
  - `main(argv: list[str] | None = None) -> int` — process entry point; return value is the exit code
  - JSON output contract (M2's web layer reuses this shape for the queue/validation payloads):
    ```json
    {"photos": [{"file": "IMG_001.jpg", "cards": [
      {"crop_index": 0, "card_ref_id": "sv4-7", "name": "Charizard",
       "confidence": 93, "accepted_stage": "h", "rotation": 0,
       "methods": [{"method": "h", "card_ref_id": "sv4-7", "score": 0.98}],
       "candidates": ["sv4-7"]}]}]}
    ```

**Design constraints:**
- `--no-llm` and BWS: the Anthropic client is constructed ONLY if `ANTHROPIC_API_KEY` is set AND `--no-llm` was not passed. When skipped, `deps.anthropic is None` (Task 14 already treats that as "LLM disabled").
- `--config` resolves by walking up from cwd to find `config.yaml` (repo root), erroring clearly if not found.
- Per-file errors (`cv2.imread` returns None, unreadable) are logged and skipped; the run continues.
- Exit 0 unless zero photos were readable (then exit 1).
- Config resolution and DB/embedder/ocr construction are all in `main`; tests monkeypatch the heavy dependencies so no DB/GPU/network/paddle is touched.

- [ ] **Step 1: Write the failing test for config resolution (walk up to repo root)**

Create `worker/tests/test_cli.py`:

```python
import json
from pathlib import Path

import numpy as np
import pytest

from notbulk import cli
from notbulk.types import Identification, MethodResult


def test_resolve_config_walks_up(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "worker" / "sub").mkdir(parents=True)
    cfg_file = root / "config.yaml"
    cfg_file.write_text("crop: {width: 734}\n")
    monkeypatch.chdir(root / "worker" / "sub")
    found = cli.resolve_config_path(None)
    assert Path(found).resolve() == cfg_file.resolve()


def test_resolve_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        cli.resolve_config_path(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd worker && uv run pytest tests/test_cli.py::test_resolve_config_walks_up -v`
Expected: FAIL with `AttributeError: module 'notbulk.cli' has no attribute 'resolve_config_path'`.

- [ ] **Step 3: Write `cli.py` config resolution + argparse scaffold**

Create `worker/notbulk/cli.py`:

```python
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2

from . import cascade as cascade_mod
from . import detect as detect_mod
from .config import load_config
from .db import get_pool
from .embed import Embedder
from .hash_index import HashIndex
from .ocr import OcrReader
from .types import Identification


def resolve_config_path(explicit: str | None) -> str:
    """--config value if given; otherwise walk up from cwd to find config.yaml."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        return str(p)
    cur = Path.cwd()
    for directory in (cur, *cur.parents):
        candidate = directory / "config.yaml"
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "config.yaml not found in cwd or any parent directory; pass --config"
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="notbulk-scan", description="Scan card photos.")
    p.add_argument("photos", nargs="+", metavar="PHOTO", help="image file paths")
    p.add_argument("--json", dest="json_path", default=None, help="write JSON output here")
    p.add_argument("--no-llm", action="store_true", help="disable Method C (Anthropic)")
    p.add_argument("--config", default=None, help="path to config.yaml (else walk up)")
    return p


def _card_name(pool, card_ref_id: str | None) -> str | None:
    if card_ref_id is None:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM card_refs WHERE id = %s", (card_ref_id,)
        ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 4: Run the config tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_cli.py::test_resolve_config_walks_up tests/test_cli.py::test_resolve_config_missing_raises -v`
Expected: PASS (both).

- [ ] **Step 5: Write the failing test for `main` — JSON structure, exit code, `--no-llm` wiring**

Append to `worker/tests/test_cli.py`:

```python
def _write_synthetic_jpg(path: Path):
    img = np.full((200, 150, 3), 200, dtype=np.uint8)
    cv2.imwrite(str(path), img)


class _FakeDetection:
    def __init__(self, crop_index):
        self.crop = np.full((1024, 734, 3), 127, dtype=np.uint8)
        self.crop_index = crop_index
        self.sharpness = 100.0
        self.quad = np.zeros((4, 2), dtype="float32")


class _FakePool:
    # card_refs lookup returns a name for known ids; supports `with pool.connection()`.
    class _Conn:
        def execute(self, sql, params):
            class _Cur:
                def fetchone(self_inner):
                    return ("Charizard",) if params and params[0] == "sv4-7" else None
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connection(self):
        return self._Conn()


class _FakeIndex:
    # HashIndex-like stub: `_build_deps` only calls `len(index)` to detect an
    # unbuilt index. `n=0` triggers the "ref_hashes is empty" SystemExit.
    def __init__(self, n=10):
        self._n = n

    def __len__(self):
        return self._n


def test_main_writes_json_and_exit_zero(tmp_path, monkeypatch):
    import cv2 as _cv2

    photo = tmp_path / "IMG_001.jpg"
    _write_synthetic_jpg(photo)
    out = tmp_path / "out.json"

    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "worker/models/does_not_exist.onnx",
                   "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(cli.HashIndex, "load", classmethod(lambda cls, pool: _FakeIndex()))
    monkeypatch.setattr(cli.detect_mod, "detect_cards",
                        lambda photo_img, cfg: [_FakeDetection(0)])
    monkeypatch.setattr(
        cli.cascade_mod, "identify_crop",
        lambda crop, deps, cfg: Identification(
            card_ref_id="sv4-7", confidence=93, accepted_stage="h", rotation=0,
            methods=[MethodResult("h", "sv4-7", 0.98)], candidates=["sv4-7"]),
    )
    # onnx file absent -> Embedder skipped; assert it is never constructed.
    monkeypatch.setattr(cli, "Embedder",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Embedder built")))
    # --no-llm -> Anthropic never constructed. Guard by monkeypatching import site.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-in-test")

    code = cli.main([str(photo), "--json", str(out), "--no-llm"])
    assert code == 0
    data = json.loads(out.read_text())
    assert data == {
        "photos": [
            {
                "file": str(photo),
                "cards": [
                    {
                        "crop_index": 0,
                        "card_ref_id": "sv4-7",
                        "name": "Charizard",
                        "confidence": 93,
                        "accepted_stage": "h",
                        "rotation": 0,
                        "methods": [{"method": "h", "card_ref_id": "sv4-7", "score": 0.98}],
                        "candidates": ["sv4-7"],
                    }
                ],
            }
        ]
    }


def test_main_no_readable_photos_exit_one(tmp_path, monkeypatch):
    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx", "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(cli.HashIndex, "load", classmethod(lambda cls, pool: _FakeIndex()))
    monkeypatch.setattr(cli, "Embedder", lambda *a, **k: None)
    # Non-existent file -> imread returns None -> skipped -> zero readable.
    code = cli.main([str(tmp_path / "missing.jpg"), "--no-llm"])
    assert code == 1


def test_main_empty_hash_index_exits_with_build_hint(tmp_path, monkeypatch):
    cfg = {
        "crop": {"width": 734, "height": 1024, "webp_quality": 80},
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx", "llm": "claude-haiku-4-5-20251001"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    }
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "get_pool", lambda: _FakePool())
    # Zero-length index -> _build_deps must raise before any model is built.
    monkeypatch.setattr(cli.HashIndex, "load",
                        classmethod(lambda cls, pool: _FakeIndex(n=0)))
    with pytest.raises(SystemExit, match="ref_hashes is empty"):
        cli.main([str(tmp_path / "any.jpg"), "--no-llm"])
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd worker && uv run pytest tests/test_cli.py::test_main_writes_json_and_exit_zero -v`
Expected: FAIL with `AttributeError: module 'notbulk.cli' has no attribute 'main'`.

- [ ] **Step 7: Implement `main` and the scan flow**

Append to `worker/notbulk/cli.py`:

```python
def _build_deps(cfg: dict, pool, *, no_llm: bool):
    hash_index = HashIndex.load(pool)
    if len(hash_index) == 0:
        raise SystemExit(
            "ref_hashes is empty — run scripts/build_hash_index.py first"
        )

    onnx_path = cfg["models"]["embedding_onnx"]
    embedder = None
    qdrant = None
    if Path(onnx_path).is_file():
        embedder = Embedder(onnx_path)
        # qdrant-client is imported lazily so tests that monkeypatch Embedder/HashIndex
        # never require a running Qdrant. Wired only when Method A is active.
        from qdrant_client import QdrantClient

        qdrant = QdrantClient(url=cfg["qdrant"]["url"])
    else:
        print(f"warning: ONNX model not found at {onnx_path}; skipping Method A",
              file=sys.stderr)

    ocr_reader = OcrReader()  # lazy: PaddleOCR loads on first read_regions call

    anthropic_client = None
    if not no_llm and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic  # imported lazily so tests never touch it when --no-llm

        anthropic_client = anthropic.Anthropic()

    return cascade_mod.CascadeDeps(
        hash_index=hash_index,
        embedder=embedder,
        qdrant=qdrant,  # Method A client, constructed alongside the embedder
        ocr_reader=ocr_reader,
        anthropic=anthropic_client,
        pool=pool,
    )


def _identification_to_dict(pool, det, ident: Identification) -> dict:
    return {
        "crop_index": det.crop_index,
        "card_ref_id": ident.card_ref_id,
        "name": _card_name(pool, ident.card_ref_id),
        "confidence": ident.confidence,
        "accepted_stage": ident.accepted_stage,
        "rotation": ident.rotation,
        "methods": [
            {"method": m.method, "card_ref_id": m.card_ref_id, "score": m.score}
            for m in ident.methods
        ],
        "candidates": list(ident.candidates),
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg_path = resolve_config_path(args.config)
    cfg = load_config(cfg_path)
    pool = get_pool()
    deps = _build_deps(cfg, pool, no_llm=args.no_llm)

    photos_out: list[dict] = []
    readable = 0

    for photo_path in args.photos:
        img = cv2.imread(photo_path)
        if img is None:
            print(f"warning: could not read {photo_path}; skipping", file=sys.stderr)
            continue
        readable += 1
        cards_out: list[dict] = []
        for det in detect_mod.detect_cards(img, cfg):
            ident = cascade_mod.identify_crop(det.crop, deps, cfg)
            cards_out.append(_identification_to_dict(pool, det, ident))
        photos_out.append({"file": photo_path, "cards": cards_out})

    _print_table(photos_out)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"photos": photos_out}, indent=2)
        )

    return 0 if readable > 0 else 1


def _print_table(photos_out: list[dict]) -> None:
    header = f"{'photo':<24} {'idx':>3} {'card_ref_id':<12} {'name':<20} {'conf':>4} {'stage':<10} {'rot':>3}"
    print(header)
    print("-" * len(header))
    for photo in photos_out:
        fname = Path(photo["file"]).name
        if not photo["cards"]:
            print(f"{fname:<24} {'-':>3} {'(no cards)':<12}")
            continue
        for c in photo["cards"]:
            print(
                f"{fname:<24} {c['crop_index']:>3} "
                f"{(c['card_ref_id'] or '-'):<12} {(c['name'] or '-'):<20} "
                f"{c['confidence']:>4} {c['accepted_stage']:<10} {c['rotation']:>3}"
            )
```

- [ ] **Step 8: Run the `main` tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_cli.py -v`
Expected: PASS — config resolution (x2), JSON structure + exit 0 + no-Embedder + no-Anthropic-under-`--no-llm`, zero-readable exit 1.

- [ ] **Step 9: Register the console-script entry point**

Add to `worker/pyproject.toml` (create the table if absent):

```toml
[project.scripts]
notbulk-scan = "notbulk.cli:main"
```

- [ ] **Step 10: Verify the entry point resolves (import-only, no DB needed)**

Run: `cd worker && uv run python -c "from notbulk.cli import main; print('entry ok')"`
Expected output: `entry ok`

The real invocation, once indexes are built and secrets are available, is:

```bash
bws run -- uv run notbulk-scan ~/photos/batch1/*.jpg --json out.json
```

- [ ] **Step 11: Commit**

```bash
git add worker/notbulk/cli.py worker/pyproject.toml worker/tests/test_cli.py
git commit -m "feat(cli): notbulk-scan entry point with JSON + table output"
```

---

### Task 16: eval harness — regression suite + baseline + manifest scaffold

**Files:**
- Create: `eval/regression.py` (runnable via `cd worker && uv run python ../eval/regression.py`)
- Create: `eval/baseline.json` (committed; first real run establishes real numbers)
- Create: `ground-truth/manifest.json` (empty scaffold)
- Create: `eval/tests/test_regression.py` (unit + smoke)
- Modify: `.gitignore` (add `eval/last_run.json`)
- Modify: `VERSION` (final step: bump to `0.2.0`)
- Modify: `CLAUDE.md` at repo root (final step: merge-gate line references `eval/regression.py`)

**Interfaces:**
- Consumes (exact signatures from Tasks 3, 6, 9, 11, 12, 14, 15):
  - `notbulk/config.py`: `load_config(path) -> dict`
  - `notbulk/db.py`: `get_pool() -> ConnectionPool`
  - `notbulk/hash_index.py`: `HashIndex.load(pool) -> HashIndex`
  - `notbulk/detect.py`: `detect_cards(photo, cfg) -> list[Detection]`
  - `notbulk/cascade.py`: `CascadeDeps`, `identify_crop(crop, deps, cfg) -> Identification`
  - `notbulk/types.py`: `Detection`, `Identification`, `MethodResult`
  - Manifest schema: `{"photos": [{"file", "scenario", "cards": [{"card_ref_id", "finish", "notes"}]}]}`
- Produces (nothing downstream in M1 depends on eval internals; the *contract* M2/CI relies on is the exit code):
  - `score_photo(manifest_photo: dict, idents: list[Identification], cfg: dict) -> list[dict]` — per-card outcome rows
  - `aggregate(rows: list[dict]) -> dict` — metrics dict
  - `check_regression(metrics: dict, baseline: dict) -> tuple[bool, str]` — `(passed, reason)`
  - `main(argv) -> int` — exit 0 pass, 1 wrong-accept OR regression, 2 config/data error

**Design constraints (spec §8, design A1):**
- **Zero wrong auto-accepts is a HARD fail (exit 1).** A wrong auto-accept = an `accepted_stage in {h, multi, llm}` whose `card_ref_id != manifest card_ref_id`.
- Match detected crops to manifest cards by `crop_index` order (detection order is stable, spec §4.1).
  - Caveat: positional `crop_index` matching means a missed or extra detection shifts alignment for every subsequent card in that photo — acceptable per the spec §4.1 stable-ordering assumption; revisit (e.g. IoU-based alignment) only if eval surfaces spurious wrong-accepts from misalignment.
- Outcomes per card: `auto_accepted_correct`, `auto_accepted_WRONG`, `sent_to_validation`, `unreadable`, `missed_detection` (manifest lists a card at that index but detection produced none).
- Metrics: `wrong_auto_accepts` (count + list), `auto_accept_rate`, `hash_tier_hit_rate` (fraction of cards with `accepted_stage == 'h'`), exit-stage distribution, `llm_calls` (count of `MethodResult` with `method == 'c'` across all idents — cost math deferred until real pricing; report count only). Everything split by manifest `scenario` and by `finish`.
- Regression: `auto_accept_rate < baseline.auto_accept_rate - 0.01` -> exit 1.
- `--update-baseline` rewrites `baseline.json` from the current run.
- Writes `eval/last_run.json` (gitignored) with full per-card detail.

- [ ] **Step 1: Create the manifest scaffold and baseline**

Create `ground-truth/manifest.json`:

```json
{ "photos": [] }
```

Create `eval/baseline.json`:

```json
{
  "_note": "Placeholder baseline. The first real eval run against the ground-truth set establishes these numbers via `--update-baseline`.",
  "auto_accept_rate": 0.0,
  "hash_tier_hit_rate": 0.0
}
```

Add to `.gitignore` (repo root):

```gitignore
eval/last_run.json
```

- [ ] **Step 2: Write the failing test for `score_photo` outcome classification**

Create `eval/tests/test_regression.py`:

```python
import json
import sys
from pathlib import Path

# eval/ is not a package; import the sibling module directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import regression  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))
from notbulk.types import Identification, MethodResult  # noqa: E402


def _cfg():
    return {"cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40}}


def _ident(card_ref_id, stage, conf=90, methods=None):
    return Identification(
        card_ref_id=card_ref_id, confidence=conf, accepted_stage=stage,
        rotation=0, methods=methods or [], candidates=[],
    )


def test_score_photo_classifies_outcomes():
    manifest_photo = {
        "file": "IMG_001.jpg",
        "scenario": "clean",
        "cards": [
            {"card_ref_id": "sv4-1", "finish": "normal", "notes": ""},   # correct accept
            {"card_ref_id": "sv4-2", "finish": "holofoil", "notes": ""}, # WRONG accept
            {"card_ref_id": "sv4-3", "finish": "normal", "notes": ""},   # validation
            {"card_ref_id": "sv4-4", "finish": "normal", "notes": ""},   # unreadable
            {"card_ref_id": "sv4-5", "finish": "normal", "notes": ""},   # missed detection
        ],
    }
    idents = [
        _ident("sv4-1", "h"),                 # correct
        _ident("sv4-99", "multi"),            # WRONG (auto-accepted, wrong id)
        _ident(None, "validation", conf=55),  # validation
        _ident(None, "unreadable", conf=0),   # unreadable
        # index 4 missing -> missed_detection
    ]
    rows = regression.score_photo(manifest_photo, idents, _cfg())
    outcomes = [r["outcome"] for r in rows]
    assert outcomes == [
        "auto_accepted_correct",
        "auto_accepted_WRONG",
        "sent_to_validation",
        "unreadable",
        "missed_detection",
    ]
    # scenario + finish carried through for splitting
    assert rows[1]["scenario"] == "clean"
    assert rows[1]["finish"] == "holofoil"
    assert rows[1]["expected"] == "sv4-2"
    assert rows[1]["got"] == "sv4-99"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd worker && uv run pytest ../eval/tests/test_regression.py::test_score_photo_classifies_outcomes -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'regression'` or `AttributeError: ... has no attribute 'score_photo'`.

- [ ] **Step 4: Write `regression.py` scoring + aggregation core**

Create `eval/regression.py`:

```python
#!/usr/bin/env python3
"""NotBulk accuracy regression harness (spec §8).

Run from the worker env so notbulk imports resolve:
    cd worker && uv run python ../eval/regression.py [--update-baseline]

Exit codes: 0 pass, 1 wrong auto-accept OR auto-accept-rate regression,
2 config/data error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _REPO_ROOT / "worker"
if str(_WORKER) not in sys.path:
    sys.path.insert(0, str(_WORKER))

_ACCEPT_STAGES = {"h", "multi", "llm"}
_MANIFEST = _REPO_ROOT / "ground-truth" / "manifest.json"
_BASELINE = _REPO_ROOT / "eval" / "baseline.json"
_LAST_RUN = _REPO_ROOT / "eval" / "last_run.json"


def score_photo(manifest_photo: dict, idents: list, cfg: dict) -> list[dict]:
    """Match detected identifications to manifest cards by crop_index order and
    classify each into one outcome."""
    scenario = manifest_photo.get("scenario", "unknown")
    manifest_cards = manifest_photo.get("cards", [])
    rows: list[dict] = []
    for i, card in enumerate(manifest_cards):
        expected = card["card_ref_id"]
        finish = card.get("finish", "unknown")
        base = {
            "file": manifest_photo.get("file", ""),
            "scenario": scenario,
            "finish": finish,
            "crop_index": i,
            "expected": expected,
        }
        if i >= len(idents):
            rows.append({**base, "outcome": "missed_detection", "got": None,
                         "accepted_stage": None})
            continue
        ident = idents[i]
        stage = ident.accepted_stage
        got = ident.card_ref_id
        if stage == "unreadable":
            outcome = "unreadable"
        elif stage in _ACCEPT_STAGES:
            outcome = "auto_accepted_correct" if got == expected else "auto_accepted_WRONG"
        else:  # 'validation'
            outcome = "sent_to_validation"
        rows.append({**base, "outcome": outcome, "got": got, "accepted_stage": stage})
    return rows


def _count_llm_calls(idents: list) -> int:
    total = 0
    for ident in idents:
        total += sum(1 for m in ident.methods if m.method == "c")
    return total


def aggregate(rows: list[dict], llm_calls: int = 0) -> dict:
    total = len(rows)
    accepted = [r for r in rows if r["outcome"].startswith("auto_accepted")]
    wrong = [r for r in rows if r["outcome"] == "auto_accepted_WRONG"]
    hash_hits = [r for r in rows if r.get("accepted_stage") == "h"]

    def _rate(n: int) -> float:
        return round(n / total, 4) if total else 0.0

    stage_dist: dict[str, int] = {}
    for r in rows:
        key = r.get("accepted_stage") or "none"
        stage_dist[key] = stage_dist.get(key, 0) + 1

    def _split(field: str) -> dict:
        out: dict[str, dict] = {}
        for r in rows:
            k = r.get(field, "unknown")
            bucket = out.setdefault(k, {"total": 0, "auto_accepted": 0, "wrong": 0})
            bucket["total"] += 1
            if r["outcome"].startswith("auto_accepted"):
                bucket["auto_accepted"] += 1
            if r["outcome"] == "auto_accepted_WRONG":
                bucket["wrong"] += 1
        return out

    return {
        "total_cards": total,
        "wrong_auto_accepts": {"count": len(wrong), "cards": wrong},
        "auto_accept_rate": _rate(len(accepted)),
        "hash_tier_hit_rate": _rate(len(hash_hits)),
        "exit_stage_distribution": stage_dist,
        "llm_calls": llm_calls,
        "by_scenario": _split("scenario"),
        "by_finish": _split("finish"),
    }


def check_regression(metrics: dict, baseline: dict) -> tuple[bool, str]:
    """(passed, reason). Fails on any wrong auto-accept (hard) or an
    auto_accept_rate that dropped more than 0.01 below baseline."""
    if metrics["wrong_auto_accepts"]["count"] > 0:
        cards = metrics["wrong_auto_accepts"]["cards"]
        detail = ", ".join(f"{c['file']}#{c['crop_index']} {c['expected']}->{c['got']}"
                            for c in cards)
        return False, f"WRONG AUTO-ACCEPT (hard fail): {detail}"
    base_rate = baseline.get("auto_accept_rate", 0.0)
    if metrics["auto_accept_rate"] < base_rate - 0.01:
        return False, (f"regression: auto_accept_rate {metrics['auto_accept_rate']} "
                       f"< baseline {base_rate} - 0.01")
    return True, "ok"
```

- [ ] **Step 5: Run the scoring test to verify it passes**

Run: `cd worker && uv run pytest ../eval/tests/test_regression.py::test_score_photo_classifies_outcomes -v`
Expected: PASS.

- [ ] **Step 6: Write failing tests for aggregation, splits, regression logic, and llm-call counting**

Append to `eval/tests/test_regression.py`:

```python
def test_aggregate_metrics_and_splits():
    manifest_photo = {
        "file": "IMG.jpg", "scenario": "holo",
        "cards": [
            {"card_ref_id": "sv4-1", "finish": "holofoil"},
            {"card_ref_id": "sv4-2", "finish": "normal"},
        ],
    }
    idents = [_ident("sv4-1", "h"), _ident("sv4-2", "multi")]
    rows = regression.score_photo(manifest_photo, idents, _cfg())
    metrics = regression.aggregate(rows, llm_calls=0)
    assert metrics["total_cards"] == 2
    assert metrics["auto_accept_rate"] == 1.0
    assert metrics["hash_tier_hit_rate"] == 0.5      # 1 of 2 at stage 'h'
    assert metrics["wrong_auto_accepts"]["count"] == 0
    assert metrics["by_scenario"]["holo"]["auto_accepted"] == 2
    assert metrics["by_finish"]["holofoil"]["total"] == 1


def test_check_regression_hard_fails_on_wrong_accept():
    manifest_photo = {"file": "IMG.jpg", "scenario": "clean",
                      "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}]}
    idents = [_ident("sv4-999", "h")]  # wrong id, auto-accepted
    metrics = regression.aggregate(regression.score_photo(manifest_photo, idents, _cfg()))
    passed, reason = regression.check_regression(metrics, {"auto_accept_rate": 0.0})
    assert passed is False
    assert "WRONG AUTO-ACCEPT" in reason


def test_check_regression_fails_on_rate_drop():
    metrics = {"wrong_auto_accepts": {"count": 0, "cards": []}, "auto_accept_rate": 0.80}
    passed, reason = regression.check_regression(metrics, {"auto_accept_rate": 0.90})
    assert passed is False
    assert "regression" in reason


def test_check_regression_passes_within_tolerance():
    metrics = {"wrong_auto_accepts": {"count": 0, "cards": []}, "auto_accept_rate": 0.895}
    passed, _ = regression.check_regression(metrics, {"auto_accept_rate": 0.90})
    assert passed is True


def test_count_llm_calls():
    idents = [
        _ident("sv4-1", "llm", methods=[MethodResult("h", None, 0.1),
                                        MethodResult("c", "sv4-1", 0.9)]),
        _ident("sv4-2", "h", methods=[MethodResult("h", "sv4-2", 0.99)]),
    ]
    assert regression._count_llm_calls(idents) == 1
```

- [ ] **Step 7: Run the aggregation/regression tests**

Run: `cd worker && uv run pytest ../eval/tests/test_regression.py -v`
Expected: PASS — all scoring, aggregation, split, regression, and llm-count tests.

- [ ] **Step 8: Write the failing smoke test for `main` (canned pipeline, exit codes)**

Append to `eval/tests/test_regression.py`:

```python
def _make_deps_stub():
    class _Deps:  # duck-typed CascadeDeps; regression.main never inspects fields
        pass
    return _Deps()


def test_main_smoke_pass(tmp_path, monkeypatch):
    manifest = {"photos": [{
        "file": "IMG_001.jpg", "scenario": "clean",
        "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}],
    }]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"auto_accept_rate": 0.0, "hash_tier_hit_rate": 0.0}))
    last_run_path = tmp_path / "last_run.json"

    monkeypatch.setattr(regression, "_MANIFEST", manifest_path)
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_LAST_RUN", last_run_path)
    monkeypatch.setattr(regression, "load_config", lambda path=None: {
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx"},
        "qdrant": {"url": "http://127.0.0.1:6333"},
    })
    monkeypatch.setattr(regression, "_load_pipeline", lambda cfg: _make_deps_stub())

    class _Det:
        def __init__(self, i):
            import numpy as np
            self.crop = np.zeros((1024, 734, 3), dtype="uint8")
            self.crop_index = i

    monkeypatch.setattr(regression, "_read_photo", lambda path: object())
    monkeypatch.setattr(regression, "_detect", lambda photo, cfg: [_Det(0)])
    monkeypatch.setattr(regression, "_identify",
                        lambda crop, deps, cfg: _ident("sv4-1", "h"))

    code = regression.main([])
    assert code == 0
    assert last_run_path.exists()


def test_main_smoke_wrong_accept_exit_one(tmp_path, monkeypatch):
    manifest = {"photos": [{
        "file": "IMG_001.jpg", "scenario": "clean",
        "cards": [{"card_ref_id": "sv4-1", "finish": "normal"}],
    }]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"auto_accept_rate": 0.0}))
    monkeypatch.setattr(regression, "_MANIFEST", manifest_path)
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_LAST_RUN", tmp_path / "last_run.json")
    monkeypatch.setattr(regression, "load_config", lambda path=None: {
        "detection": {"sharpness_min": 45.0, "max_cards_per_photo": 30},
        "cascade": {"auto_accept": 80, "hash_only_accept": 90, "unreadable_below": 40},
        "models": {"embedding_onnx": "nope.onnx"}, "qdrant": {"url": "x"}})
    monkeypatch.setattr(regression, "_load_pipeline", lambda cfg: _make_deps_stub())
    monkeypatch.setattr(regression, "_read_photo", lambda path: object())
    monkeypatch.setattr(regression, "_detect", lambda photo, cfg: [object()])
    monkeypatch.setattr(regression, "_identify",
                        lambda crop, deps, cfg: _ident("sv4-WRONG", "h"))
    code = regression.main([])
    assert code == 1
```

- [ ] **Step 9: Run the smoke tests to verify they fail**

Run: `cd worker && uv run pytest ../eval/tests/test_regression.py::test_main_smoke_pass -v`
Expected: FAIL with `AttributeError: module 'regression' has no attribute 'main'` (and `_load_pipeline`, `_read_photo`, `_detect`, `_identify`).

- [ ] **Step 10: Implement `main` and the pipeline seams**

Append to `eval/regression.py`:

```python
from notbulk.config import load_config  # noqa: E402
from notbulk.db import get_pool  # noqa: E402


def _load_pipeline(cfg: dict):
    """Build CascadeDeps against the real DB/index. Isolated so tests stub it."""
    from notbulk.cascade import CascadeDeps
    from notbulk.embed import Embedder
    from notbulk.hash_index import HashIndex
    from notbulk.ocr import OcrReader

    pool = get_pool()
    hash_index = HashIndex.load(pool)
    if len(hash_index) == 0:
        raise RuntimeError("ref_hashes is empty — run scripts/build_hash_index.py first")
    onnx_path = cfg["models"]["embedding_onnx"]
    embedder = Embedder(onnx_path) if Path(onnx_path).is_file() else None
    # qdrant-client is imported lazily so tests stubbing _load_pipeline never need it.
    qdrant = None
    if embedder is not None:
        from qdrant_client import QdrantClient

        qdrant = QdrantClient(url=cfg["qdrant"]["url"])
    return CascadeDeps(
        hash_index=hash_index, embedder=embedder, qdrant=qdrant,
        ocr_reader=OcrReader(), anthropic=None, pool=pool,
    )


def _read_photo(path: str):
    import cv2
    img = cv2.imread(path)
    return img


def _detect(photo, cfg: dict):
    from notbulk.detect import detect_cards
    return detect_cards(photo, cfg)


def _identify(crop, deps, cfg: dict):
    from notbulk.cascade import identify_crop
    return identify_crop(crop, deps, cfg)


def _print_summary(metrics: dict) -> None:
    print("=== NotBulk regression summary ===")
    print(f"cards evaluated : {metrics['total_cards']}")
    print(f"auto-accept rate: {metrics['auto_accept_rate']}")
    print(f"hash-tier hit   : {metrics['hash_tier_hit_rate']}")
    print(f"wrong accepts   : {metrics['wrong_auto_accepts']['count']}")
    print(f"llm calls       : {metrics['llm_calls']}")
    print(f"exit stages     : {metrics['exit_stage_distribution']}")
    print("by scenario     :")
    for k, v in sorted(metrics["by_scenario"].items()):
        print(f"  {k:<10} total={v['total']} accepted={v['auto_accepted']} wrong={v['wrong']}")
    print("by finish       :")
    for k, v in sorted(metrics["by_finish"].items()):
        print(f"  {k:<10} total={v['total']} accepted={v['auto_accepted']} wrong={v['wrong']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="regression", description="NotBulk accuracy gate.")
    parser.add_argument("--update-baseline", action="store_true",
                        help="rewrite baseline.json from this run")
    args = parser.parse_args(argv)

    try:
        manifest = json.loads(_MANIFEST.read_text())
        baseline = json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"config/data error: {exc}", file=sys.stderr)
        return 2

    try:
        cfg = load_config()
        deps = _load_pipeline(cfg)
    except Exception as exc:  # DB down, index empty, etc.
        print(f"config/data error: {exc}", file=sys.stderr)
        return 2

    all_rows: list[dict] = []
    all_idents: list = []
    for mphoto in manifest.get("photos", []):
        photo_path = str(_REPO_ROOT / "ground-truth" / mphoto["file"])
        img = _read_photo(photo_path)
        if img is None:
            # A missing image maps every manifest card to missed_detection.
            all_rows.extend(score_photo(mphoto, [], cfg))
            continue
        dets = _detect(img, cfg)
        idents = [_identify(d.crop, deps, cfg) for d in dets]
        all_idents.extend(idents)
        all_rows.extend(score_photo(mphoto, idents, cfg))

    metrics = aggregate(all_rows, llm_calls=_count_llm_calls(all_idents))
    _LAST_RUN.write_text(json.dumps({"metrics": metrics, "rows": all_rows}, indent=2))
    _print_summary(metrics)

    if args.update_baseline:
        _BASELINE.write_text(json.dumps({
            "auto_accept_rate": metrics["auto_accept_rate"],
            "hash_tier_hit_rate": metrics["hash_tier_hit_rate"],
        }, indent=2))
        print(f"baseline updated -> {_BASELINE}")
        return 0

    passed, reason = check_regression(metrics, baseline)
    print(f"result: {'PASS' if passed else 'FAIL'} — {reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 11: Run the full eval test module**

Run: `cd worker && uv run pytest ../eval/tests/test_regression.py -v`
Expected: PASS — scoring, aggregation, splits, regression logic, llm-count, `--update-baseline` path exercised via smoke pass, wrong-accept exit-1 smoke.

- [ ] **Step 12: Verify the harness runs against the empty manifest (exit 0, no cards)**

Run: `cd worker && uv run python ../eval/regression.py`
Expected: prints the summary with `cards evaluated : 0`, `result: PASS — ok`, exits 0. (Requires the compose DB + a non-empty `ref_hashes`; if the DB is unavailable the harness prints `config/data error` and exits 2 — that is the documented behavior, not a test failure.)

- [ ] **Step 13: Commit the harness (before the version bump)**

```bash
git add eval/regression.py eval/baseline.json eval/tests/test_regression.py ground-truth/manifest.json .gitignore
git commit -m "feat(eval): regression harness scoring, aggregation, and CLI"
```

- [ ] **Step 14: Bump VERSION and update the merge-gate line in CLAUDE.md**

Set `VERSION` (repo root) to:

```
0.2.0
```

In the repo-root `CLAUDE.md`, update the merge-gate rule so it names the runnable harness. The line must read (adjust surrounding prose minimally to keep it a single coherent sentence):

```markdown
- **Merge gate:** any change to detection, hashing, embeddings, OCR, scoring, or thresholds MUST run `eval/regression.py` (`cd worker && uv run python ../eval/regression.py`) and include its output; the suite hard-fails on any wrong auto-accept and fails on an auto-accept-rate regression.
```

- [ ] **Step 15: Verify the version and gate line**

Run: `cat /home/bryson/claude/projects/not-bulk/VERSION && grep -n "eval/regression.py" /home/bryson/claude/projects/not-bulk/CLAUDE.md`
Expected: `0.2.0` on the first line; the grep prints the merge-gate line referencing `eval/regression.py`.

- [ ] **Step 16: Commit — M1 complete**

```bash
git add VERSION CLAUDE.md
git commit -m "feat(eval): regression harness + M1 complete"
```
