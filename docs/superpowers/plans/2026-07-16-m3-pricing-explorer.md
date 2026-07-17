# M3 — Pricing + Collection Explorer + CSV Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user's validated cards become a priced, browsable, exportable collection: prices fetched from pokemontcg.io (Collectr pluggable), cached in Postgres; a collection explorer with sort/filter/stats; reference-card thumbnails served through a locally-caching image proxy; and a streamed CSV export.

**Architecture:** Pricing runs in the Python worker as a new `price` job type (the identify handler enqueues a price job per resolved card); a pluggable `PriceSource` abstraction tries sources in config order and upserts the `prices` cache. The Node web layer only READS the `prices` cache to render the explorer and stream CSV. Reference images (pokemontcg.io `image_url`) are proxied and cached into MinIO by Node so the CSP stays `self`+MinIO and thumbnails survive a pokemontcg.io outage (reference-data-independence theme).

**Tech Stack:** unchanged from M1/M2 — Python 3.11 (uv) worker + Node 20 (pnpm) Express/TS/Nunjucks/htmx web, Postgres 16 (:5434), MinIO (:9000). New Python dep: none (httpx already present). New Node dep: none (@aws-sdk/client-s3 already present for the proxy cache).

## Global Constraints

- Secrets ONLY via `bws run`. pokemontcg.io pricing uses `POKEMONTCG_API_KEY` (already the M1 secret; raises the rate limit but pricing works keyless at low volume). Collectr uses `COLLECTR_API_KEY` (not yet provisioned — the Collectr source stays a stub that reports "not configured" until access lands).
- **Zero wrong auto-accepts remains the hard invariant.** The finish-spread narrowing (Task 8) may only CLEAR `finish_needs_confirmation` / downgrade a validation-due-to-finish card toward auto — it must never change a card's identity, never touch a `validated`/`corrected`/`skipped`/`not_card`/`merged` card, and never clear the flag when the price spread is unknown or >15%. The eval suite (`cd worker && uv run python ../eval/regression.py`) must still pass (exit 2 acceptable while ref_hashes is empty).
- **Pricing correctness:** a missing/failed price is stored as `price IS NULL` (a cached known-miss, so we don't re-hit the API within TTL) and rendered as "no price data" — NEVER `$0` (spec §5). Every price row records `source`, `finish`, `fetched_at`. Refetch threshold 24h; physical GC of price rows is M4 janitor work (not M3).
- **Ownership scoping (AC 7) unchanged:** every explorer/export query filters by `user_id` from the session; no bare-id lookups.
- **Reference-image proxy is NOT an SSRF surface:** it fetches ONLY `card_refs.image_url` values (trusted, populated from pokemontcg.io at index-build time, never user input) and only when the host is exactly `images.pokemontcg.io`. It is not the user-URL fetcher (that's M2's worker-side SSRF gate); a hardcoded single-host check is the control here.
- CSP unchanged (`img-src 'self' http://127.0.0.1:9000`) — the proxy serves cached images from MinIO, so no third-party img host is added.
- All money is handled as integer **cents** end to end (never float dollars); `prices.price_cents integer`, NULL = no data. Display formats to `$X.XX` at the view/CSV boundary only.
- All IDs uuidv7. Conventional commits. VERSION bump to 0.4.0 ONLY in the final task.
- Web tests: `cd web && pnpm vitest run` (Node 20 — `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"`). Worker tests: `cd worker && uv run pytest tests ../eval/tests`. Structured image fixtures only (never smooth gradients/noise — DCT-hash pathology).
- dbmate: `DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up` from repo root.
- **Out of scope for M3** (do not build): PDF export (M4, Puppeteer), the M4 janitor/price-GC, per-user LLM sub-caps (M5 hardening), Collectr live integration (stub only), graded pricing (V2).

## File Structure

```
migrations/003_m3_prices.sql                              # Task 1

worker/notbulk/
├── pricing/
│   ├── __init__.py
│   ├── sources.py         # PriceSource protocol, PokemonTcgPriceSource, CollectrPriceSource(stub), resolve_price   Task 2
│   └── cache.py           # read_cached / upsert_price (prices table), TTL check                                     Task 2
├── handlers/price.py      # handle_price job                                                                          Task 3
├── handlers/identify.py   # MODIFY: enqueue price jobs for the resolved card's finishes                              Task 4
└── handlers/finish.py     # finish-spread narrowing (invoked by handle_price after all finishes priced)              Task 8

web/src/
├── services/refproxy.ts   # ensureRefCached(cardRefId) -> MinIO key; fetch images.pokemontcg.io, cache               Task 5
├── routes/refimg.ts       # GET /img/ref/:cardRefId -> 302 signed MinIO URL                                          Task 5
├── routes/collection.ts   # GET /collection (explorer) + GET /collection/export.csv                                  Tasks 6,7
├── queries/collection.ts  # owned-scope collection query (cards JOIN card_refs LEFT JOIN prices), stats              Task 6
├── views/collection.njk   # explorer grid + stats bar + sort/filter controls                                        Task 6
├── public/js/collection.js# CSP-safe sort/filter (htmx or query-param links; no inline)                             Task 6
└── views/partials/...      # MODIFY validate.njk to show a reference thumbnail via /img/ref/:cardRefId               Task 9
```

## Interface Contract (authoritative — all tasks conform exactly)

### Migration 003 (dbmate up/down)

```sql
-- migrate:up
CREATE TABLE prices (
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  finish text NOT NULL,                 -- tcgplayer finish key: 'normal' | 'holofoil' | 'reverseHolofoil' | ...
  price_cents integer,                  -- NULL = cached known-miss (no data), NOT $0
  source text NOT NULL,                 -- 'pokemontcg' | 'collectr'
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (card_ref_id, finish)
);

-- extend jobs.type to allow 'price' (recreate the CHECK)
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));

-- reference-image cache marker: which card_refs have a MinIO-cached ref image
ALTER TABLE card_refs ADD COLUMN ref_cached_key text;   -- NULL until proxied+cached once

-- migrate:down
ALTER TABLE card_refs DROP COLUMN ref_cached_key;
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction'));
DROP TABLE prices;
```

### config.yaml additions (Task 1)

```yaml
pricing:
  source_order: ["pokemontcg"]          # 'collectr' prepended here once COLLECTR_API_KEY is provisioned
  cache_ttl_hours: 24
  finish_spread_flag_pct: 15            # >15% price spread across finishes keeps finish_needs_confirmation
  pokemontcg_base: "https://api.pokemontcg.io/v2"
explorer:
  page_size: 60
  default_sort: "value_desc"            # value_desc | name_asc | set_asc
refproxy:
  allowed_image_host: "images.pokemontcg.io"
  cache_prefix: "refs"                  # MinIO key prefix: refs/{card_ref_id}.webp
  max_bytes: 5242880                    # 5 MB cap on a reference-image fetch
```

### Python signatures (worker)

```python
# notbulk/pricing/sources.py
FINISH_KEYS = ("normal", "holofoil", "reverseHolofoil")   # tcgplayer price keys, verbatim (matches card_refs.finishes)

class PriceSource(Protocol):
    name: str
    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None: ...
        # returns price in CENTS, or None for a genuine miss; raises SourceUnavailable on transient failure

class SourceUnavailable(Exception): ...
class SourceNotConfigured(SourceUnavailable): ...

class PokemonTcgPriceSource:
    name = "pokemontcg"
    def fetch(self, card_ref_id, finish, cfg) -> int | None: ...
        # GET {pokemontcg_base}/cards/{card_ref_id}, read data.tcgplayer.prices[finish].market (USD float)
        # -> round(market*100) cents; None if the finish key or market is absent; X-Api-Key from
        #    POKEMONTCG_API_KEY env when set. Raises SourceUnavailable on 429/5xx/transport.

class CollectrPriceSource:
    name = "collectr"
    def fetch(self, card_ref_id, finish, cfg) -> int | None:
        raise SourceNotConfigured("collectr access pending")   # stub until COLLECTR_API_KEY provisioned

def resolve_price(sources: list[PriceSource], card_ref_id: str, finish: str, cfg: dict) -> tuple[int | None, str]:
    ...  # try sources in order; first that returns (int|None without raising) wins -> (cents_or_None, source_name);
         # SourceUnavailable/SourceNotConfigured -> skip to next; all unavailable -> raise SourceUnavailable

# notbulk/pricing/cache.py
def read_cached(pool, card_ref_id: str, finish: str, ttl_hours: int) -> tuple[bool, int | None]:
    ...  # (fresh, price_cents): fresh=True if a row exists with fetched_at within ttl (incl. NULL known-miss);
         # (False, None) if absent or stale
def upsert_price(pool, card_ref_id: str, finish: str, price_cents: int | None, source: str) -> None:
    ...  # INSERT ... ON CONFLICT (card_ref_id, finish) DO UPDATE SET price_cents, source, fetched_at=now()

# notbulk/handlers/price.py
def handle_price(pool, storage, payload: dict, cfg: dict) -> None:
    ...  # payload {card_ref_id, finish}: read_cached -> if fresh, done; else resolve_price via configured
         # sources -> upsert (NULL on genuine miss; SourceUnavailable -> raise to retry the job).
         # After upsert, call finish.maybe_narrow_finish_flag(pool, card_ref_id, cfg) (Task 8).

# notbulk/handlers/finish.py
def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None:
    ...  # Only for cards WHERE card_ref_id=? AND status='validation' AND finish_needs_confirmation
         # AND accepted_stage IN ('h','multi','llm'):  compute price spread across the card's finishes
         # from the prices cache. If ALL relevant finishes are priced (no NULL/missing) AND
         # spread_pct <= finish_spread_flag_pct: clear finish_needs_confirmation, set finish to the
         # cheapest-priced finish's key... NO -> set finish to the card_refs single most-common finish?
         # AUTHORITATIVE RULE: pick the finish with the HIGHEST cached price is wrong; instead, when
         # spread<=15% the finish barely affects value, so set finish = the FIRST of card_refs.finishes
         # in FINISH_KEYS order that is priced, set status='auto', finish_needs_confirmation=false,
         # accepted_stage unchanged. If any finish price is NULL/missing, or spread>15%, or spread
         # cannot be computed: LEAVE THE CARD UNTOUCHED (stays in validation). Never touch a card
         # whose status is not 'validation' or whose accepted_stage is 'validation'/'unreadable'/None.
```

### Node signatures (web)

```ts
// src/services/refproxy.ts
export async function ensureRefCached(pool: Pool, storage: Storage, cfg: Config, cardRefId: string): Promise<string | null>;
  // returns the MinIO key (refs/{cardRefId}.webp), caching on first call:
  //   SELECT ref_cached_key, image_url FROM card_refs WHERE id=$1  (owner-agnostic: card_refs is global reference data)
  //   if ref_cached_key set -> return it
  //   else: parse image_url; REJECT unless hostname === cfg.refproxy.allowed_image_host; fetch (https, no redirects,
  //         size cap, image content-type), sharp -> webp, storage.put(refs/{id}.webp), UPDATE card_refs.ref_cached_key,
  //         return key. On any failure -> return null (caller renders a placeholder). NULL card_ref_id -> null.

// src/routes/refimg.ts
// GET /img/ref/:cardRefId  (requireUser) -> ensureRefCached -> 302 signed MinIO URL, or 404 if null.
//   Note: reference images are global/non-owned (unlike /img/crop which is owner-scoped); requireUser gates
//   access to authenticated users but there is no per-user ownership on reference art.

// src/queries/collection.ts
export interface CollectionRow {
  card_id: string; card_ref_id: string | null; crop_storage_key: string | null;
  name: string | null; set_name: string | null; number: string | null;
  finish: string | null; quantity: number; confidence: number;
  status: string;                        // auto | validated | corrected (explorer shows these only)
  price_cents: number | null; price_source: string | null; price_fetched_at: string | null;
  has_price_row: boolean;         // false = no prices row yet (pending); true+null price_cents = cached no-data
  batch_id: string;
}
export interface CollectionStats { total_cards: number; total_value_cents: number; priced_fraction: number; oldest_price_at: string | null }
export async function getCollection(pool: Pool, userId: string, opts: {
  batchId?: string; set?: string; finish?: string; source?: 'auto'|'corrected'; sort: string; limit: number; offset: number;
}): Promise<CollectionRow[]>;   // JOIN cards->photos->batches(user_id=$) JOIN card_refs LEFT JOIN prices(finish match)
                                //   status IN ('auto','validated','corrected'); sort value_desc|name_asc|set_asc
export async function getCollectionStats(pool: Pool, userId: string, opts): Promise<CollectionStats>;

// Routes (all requireUser, owner-scoped):
//   GET /collection            -> render collection.njk (grid + stats + sort/filter via query params)
//   GET /collection/export.csv -> streamed text/csv, one row per card, columns per §6.6
```

### CSV export columns (Task 7, §6.6 — verbatim order)

`name, set, number, finish, quantity, price, price_source, price_date, confidence, batch, image_filename`
- `price` = `$X.XX` from price_cents, or empty string when NULL (never `$0.00`).
- `image_filename` = the crop storage key basename (`{card_id}.webp`).
- One row per card row (merged/quantity handled by the `quantity` column; `merged`/`skipped`/`not_card`/`pending`/`validation`/`unreadable` cards are excluded — only `auto`/`validated`/`corrected`).
- Proper CSV quoting (fields with commas/quotes/newlines quoted per RFC 4180). Streamed, not buffered.

### Amendments to prior design (authoritative for M3)

1. **Reference thumbnails ship via the /img/ref proxy** (design M2 amendment #6 / Resolution 9 deferral resolved): validation UI and explorer detail use `/img/ref/:cardRefId`; CSP stays `self`+MinIO. The explorer GRID uses the user's own crop (`/img/crop/:id`) as the primary thumbnail (consistent with PDF export using crops, §6.6); reference art is secondary.
2. **Finish-spread narrowing** (design §4.4 / M2 amendment #3): implemented in Task 8 with the strict invariant guard above. Prices must exist for all of a card's finishes before the flag can be cleared; otherwise the card stays in validation.
3. **Pricing is worker-side** (`price` job); the identify handler enqueues one `price` job per finish of the resolved card. The explorer/CSV only read the cache. A card with no cached price yet renders "pending price" (distinct from "no price data" = a cached NULL).
4. **Money is integer cents** throughout; the M2 design's `prices.price` becomes `prices.price_cents`.

## Assembly Resolutions (authoritative — override any conflicting task prose)

1. **`handlers/finish.py` is created as a safe no-op stub in Task 3** (so Task 3 imports cleanly and is independently committable) and **REPLACED/expanded by Task 8** with the real `maybe_narrow_finish_flag`. Task 8's implementer expands the existing file (its `git add` includes the already-tracked stub), not creates a new one.
2. **`PokemonTcgPriceSource` takes an injectable `client` (+ `max_retries`/`backoff_base`) constructor seam** so tests use `httpx.MockTransport` — mirrors the M1 embedder's injectable-session pattern. Synchronous `fetch` per the contract; the download_refs async retry idiom is adapted to sync.
3. **Finish-narrowing updates ALL qualifying candidate cards for the `card_ref_id`** in one pass (prices are per-(card_ref_id, finish) and identical across every holder of that card; the atomic guard WHERE re-checks `status='validation' AND finish_needs_confirmation AND accepted_stage IN ('h','multi','llm')` per row so a concurrent user-validation can't be clobbered). NOT filtered to only the triggering card.
4. **The narrowing UPDATE sets `status='auto'` as a literal** — safe because candidates are already restricted to accepted_stage h/multi/llm (which `_status_for_stage` maps to 'auto'). It never changes `card_ref_id`, `confidence`, `accepted_stage`, or `candidates`.
5. **`imagesRouter` signature becomes `imagesRouter(pool, storage, cfg)`** (cfg threaded for the refproxy); the `/img/ref/:cardRefId` route is added to the EXISTING imagesRouter (already `/img`-scoped + requireUser-gated), NOT a second router. Update the `app.ts` mount and `images.test.ts` accordingly (makeDeps already supplies cfg).
6. **`CollectionRow` gains `has_price_row: boolean`** (`SELECT (pr.card_ref_id IS NOT NULL) AS has_price_row`) to distinguish "pending price" (no prices row yet) from "no price data" (row with NULL price_cents). Header contract's CollectionRow is extended with this field.
7. **`CollectionFilters` type** = `{ batchId?, set?, finish?, source?: 'auto'|'corrected' }`; `getCollection` opts additionally carries `sort/limit/offset`; `getCollectionStats` and `getCollectionForExport` take `CollectionFilters` (no sort/limit/offset). A shared module-private `whereClause` builder keeps the three DRY.
8. **Money is integer cents everywhere**; `formatCents(cents: number): string` (non-null only, `web/src/lib/money.ts`, shared by Task 6's route and Task 7's CSV) -> `$X.XX` via `(cents/100).toFixed(2)`. Callers branch on null themselves: `'no price data'` (view, has a prices row but NULL price_cents) / `'pending price'` (view, no prices row) / `''` (CSV) — NEVER `$0.00`. `collection.njk` renders pre-formatted `row.price_display` / `stats.total_value_display` strings; no arithmetic in the template. pg returns SUM/COUNT as strings; stats coerce with `Number(...)`.
9. **`NOTBULK_STUB_PRICE` seam** in `handle_price` (Task 10) sits AFTER `read_cached` (cache short-circuit still wins) and BEFORE `upsert_price`/narrowing; env-gated, inert unset, returns canned `(1234,'pokemontcg')` in place of `resolve_price`. Its local-variable names must match Task 3's final `handle_price`.
10. **CSV filename is static `notbulk-collection.csv`** (no clock in plan context); `Content-Type` assertions use `toContain('text/csv')` to survive charset suffixes.
11. **E2E ref-image seeding is hermetic**: pre-seed `card_refs.ref_cached_key` + a real MinIO object so `/img/ref` 302s with no network fetch; the fetch/allowlist branch is covered by Task 5's unit tests, not the E2E.
12. **`price` in `jobqueue._REQUIRED_KEYS`** = `{'card_ref_id','finish'}` and in the migration-003 `jobs.type` CHECK — Tasks 1/3/4 all touch this; whichever lands first adds it, the others are idempotent (no duplicate).

---

<!-- TASK SECTIONS ASSEMBLED FROM PARALLEL DRAFTS BELOW -->
<!-- M3 Part 1 — worker-side pricing pipeline (Tasks 1–4). Assembled into
     docs/superpowers/plans/2026-07-16-m3-pricing-explorer.md; the plan header
     (Global Constraints / File Structure / Interface Contract) is authoritative
     and every task below conforms to it. -->

### Task 1: Migration 003 (prices table, `price` job type, ref-image cache column) + config blocks

**Files:**
- Create: `migrations/003_m3_prices.sql`
- Modify: `config.yaml` (append `pricing:` / `explorer:` / `refproxy:` blocks)
- Modify (generated): `db/schema.sql` (dbmate rewrites it on `up`)

**Interfaces:**
- Consumes: existing `card_refs(id text PK, ..., finishes text[])` and
  `jobs(type text CHECK (type IN ('detect','identify','fetch_source','ingest_correction')))`
  from `migrations/002_m2_app_tables.sql`.
- Produces: the `prices` table, the extended `jobs_type_check` allowing `'price'`,
  and `card_refs.ref_cached_key text` — every later M3 task depends on these.

- [ ] **Step 1: Write the migration file**

Create `migrations/003_m3_prices.sql` with EXACTLY the DDL from the Interface Contract.
The whole `-- migrate:up` body runs inside dbmate's single transaction, so the
`DROP CONSTRAINT` / `ADD CONSTRAINT` swap is atomic — the CHECK is never absent to a
concurrent reader, and it re-validates against any existing `jobs` rows as one unit
(if a row already violated the new set the ALTER would roll the whole migration back;
in practice all existing rows carry the four M2 types, which the new CHECK still allows).
The `-- migrate:down` reverses all three changes IN ORDER: drop the added column,
restore the old `jobs` CHECK, drop the `prices` table last (dropping `prices` first
would be fine too, but this order mirrors the up-block in reverse for reviewability).

```sql
-- migrate:up
CREATE TABLE prices (
  card_ref_id text NOT NULL REFERENCES card_refs(id) ON DELETE CASCADE,
  finish text NOT NULL,                 -- tcgplayer finish key: 'normal' | 'holofoil' | 'reverseHolofoil' | ...
  price_cents integer,                  -- NULL = cached known-miss (no data), NOT $0
  source text NOT NULL,                 -- 'pokemontcg' | 'collectr'
  fetched_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (card_ref_id, finish)
);

-- extend jobs.type to allow 'price' (recreate the CHECK)
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));

-- reference-image cache marker: which card_refs have a MinIO-cached ref image
ALTER TABLE card_refs ADD COLUMN ref_cached_key text;   -- NULL until proxied+cached once

-- migrate:down
ALTER TABLE card_refs DROP COLUMN ref_cached_key;
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction'));
DROP TABLE prices;
```

- [ ] **Step 2: Apply the migration**

Run (from repo root):

```bash
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
```

Expected output (dbmate prints the applied file and regenerates the schema snapshot):

```
Applying: 003_m3_prices.sql
Writing: ./db/schema.sql
```

- [ ] **Step 3: Verify the table + column + constraint in psql**

Run:

```bash
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  -c '\d prices' \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='card_refs' AND column_name='ref_cached_key';" \
  -c "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='jobs_type_check';"
```

Expected: `\d prices` lists columns `card_ref_id, finish, price_cents, source, fetched_at`
with primary key `(card_ref_id, finish)`; the `card_refs` query returns one row `ref_cached_key`;
the constraint def contains `'price'` in the `type IN (...)` list.

- [ ] **Step 4: Verify the CHECK swap is safe against existing job rows**

The dev DB may have zero `jobs` rows, so the swap is trivially safe — but prove the ALTER
is transactional and re-validating (a would-be-illegal existing row would abort the whole
migration, not leave a half-applied CHECK). Insert a legal probe row, confirm it survives,
and confirm an illegal type is rejected by the NEW constraint:

```bash
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  -c "SELECT count(*) AS jobs_rows FROM jobs;" \
  -c "INSERT INTO jobs (id, type, payload) VALUES (gen_random_uuid(), 'price', '{}'::jsonb);" \
  -c "SELECT 'illegal-rejected' WHERE NOT EXISTS (SELECT 1);" \
  -c "DELETE FROM jobs WHERE type='price' AND payload='{}'::jsonb;"
```

Expected: the `INSERT ... 'price'` succeeds (`INSERT 0 1`) — proving the new CHECK admits
`'price'`. Then confirm the constraint still rejects a bogus type:

```bash
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  -c "INSERT INTO jobs (id, type, payload) VALUES (gen_random_uuid(), 'bogus', '{}'::jsonb);"
```

Expected: `ERROR:  new row for relation "jobs" violates check constraint "jobs_type_check"`.

- [ ] **Step 5: Round-trip the migration (down then up)**

Run:

```bash
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate down
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
```

Expected: `down` prints `Rolling back: 003_m3_prices.sql` and leaves the M2 schema
(no `prices`, no `ref_cached_key`, jobs CHECK back to the four M2 types); `up` re-applies
cleanly. `db/schema.sql` after the final `up` again contains the `prices` table and the
five-type CHECK. Confirm with:

```bash
grep -c 'CREATE TABLE public.prices' db/schema.sql
grep -c "'price'" db/schema.sql
```

Expected: each prints `1` (or higher for the second — at least the CHECK line matches).

- [ ] **Step 6: Append the config blocks**

Append to `config.yaml` (top-level keys, after the existing `turnstile:` line), VERBATIM
from the Interface Contract — no reflowing of the inline comments:

```yaml
pricing:
  source_order: ["pokemontcg"]          # 'collectr' prepended here once COLLECTR_API_KEY is provisioned
  cache_ttl_hours: 24
  finish_spread_flag_pct: 15            # >15% price spread across finishes keeps finish_needs_confirmation
  pokemontcg_base: "https://api.pokemontcg.io/v2"
explorer:
  page_size: 60
  default_sort: "value_desc"            # value_desc | name_asc | set_asc
refproxy:
  allowed_image_host: "images.pokemontcg.io"
  cache_prefix: "refs"                  # MinIO key prefix: refs/{card_ref_id}.webp
  max_bytes: 5242880                    # 5 MB cap on a reference-image fetch
```

- [ ] **Step 7: Confirm the config edit is valid YAML and the suite is still green**

Run:

```bash
cd worker && uv run python -c "from notbulk.config import load_config; c = load_config('../config.yaml'); print(c['pricing']['source_order'], c['pricing']['cache_ttl_hours'], c['refproxy']['allowed_image_host'])"
```

Expected: `['pokemontcg'] 24 images.pokemontcg.io`

Then run the full worker + eval suite (adding config keys must not perturb any existing test):

```bash
cd worker && uv run pytest tests ../eval/tests -q
```

Expected: `179 passed, 4 skipped` (the M2 baseline — unchanged; Task 1 adds no code paths).

- [ ] **Step 8: Commit**

```bash
git add migrations/003_m3_prices.sql db/schema.sql config.yaml
git commit -m "feat(db): M3 prices table, price job type, ref-image cache column"
```

(VERSION is bumped only in the final M3 task per the plan header — do NOT touch it here.)

---

### Task 2: `pricing/sources.py` + `pricing/cache.py`

**Files:**
- Create: `worker/notbulk/pricing/__init__.py`
- Create: `worker/notbulk/pricing/sources.py`
- Create: `worker/notbulk/pricing/cache.py`
- Test: `worker/tests/test_pricing_sources.py`
- Test: `worker/tests/test_pricing_cache.py`

**Interfaces:**
- Consumes: `httpx` (already a dep, `httpx>=0.28.1`); the sync `httpx.Client` + `httpx.MockTransport`
  test idiom mirrors `worker/scripts/download_refs.py` (429/5xx-retry, `X-Api-Key` header). The
  `FakePool` / `FakeCursor` doubles from `worker/tests/fakes.py`.
- Produces (exact signatures other tasks rely on):
  - `FINISH_KEYS: tuple[str, ...] = ("normal", "holofoil", "reverseHolofoil")`
  - `class SourceUnavailable(Exception)`, `class SourceNotConfigured(SourceUnavailable)`
  - `class PriceSource(Protocol)` with `name: str` and `fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None`
  - `PokemonTcgPriceSource(name="pokemontcg")`, `CollectrPriceSource(name="collectr")`
  - `resolve_price(sources: list[PriceSource], card_ref_id: str, finish: str, cfg: dict) -> tuple[int | None, str]`
  - `cache.read_cached(pool, card_ref_id: str, finish: str, ttl_hours: int) -> tuple[bool, int | None]`
  - `cache.upsert_price(pool, card_ref_id: str, finish: str, price_cents: int | None, source: str) -> None`

- [ ] **Step 1: Create the package marker**

Create `worker/notbulk/pricing/__init__.py`:

```python
"""Pluggable pricing sources and the prices cache (M3)."""
```

- [ ] **Step 2: Write the failing tests for `sources.py`**

Create `worker/tests/test_pricing_sources.py`. `PokemonTcgPriceSource` accepts an injectable
`client` so a `MockTransport`-backed `httpx.Client` can be handed in (same seam as
`download_refs`, but sync). The fixture JSON is a real `tcgplayer.prices` shape.

```python
"""PokemonTcgPriceSource (httpx.MockTransport, no network), the Collectr stub,
and resolve_price ordering. No real network, no DB."""
from __future__ import annotations

import httpx
import pytest

from notbulk.pricing import sources as S

CFG = {"pricing": {"pokemontcg_base": "https://api.pokemontcg.io/v2"}}

# A real /v2/cards/{id} response shape: data.tcgplayer.prices[finish].market (USD float).
_CARD_JSON = {
    "data": {
        "id": "sv4-123",
        "name": "Charizard ex",
        "tcgplayer": {
            "prices": {
                "normal": {"market": 1.20},
                "holofoil": {"market": 12.34},
            }
        },
    }
}
_CARD_NO_HOLO = {
    "data": {"id": "sv4-5", "name": "Pikachu", "tcgplayer": {"prices": {"normal": {"market": 0.10}}}}
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pokemontcg_holofoil_market_to_cents():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/cards/sv4-123"
        return httpx.Response(200, json=_CARD_JSON, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-123", "holofoil", CFG) == 1234   # 12.34 USD -> 1234 cents


def test_pokemontcg_missing_finish_key_is_a_miss():
    def handler(request):
        return httpx.Response(200, json=_CARD_NO_HOLO, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-5", "holofoil", CFG) is None      # finish key absent -> None (genuine miss)


def test_pokemontcg_missing_market_field_is_a_miss():
    def handler(request):
        body = {"data": {"tcgplayer": {"prices": {"holofoil": {"low": 5.0}}}}}   # no 'market'
        return httpx.Response(200, json=body, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("sv4-123", "holofoil", CFG) is None


def test_pokemontcg_no_tcgplayer_block_is_a_miss():
    def handler(request):
        return httpx.Response(200, json={"data": {"id": "x"}}, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    assert src.fetch("x", "normal", CFG) is None


def test_pokemontcg_429_raises_source_unavailable():
    def handler(request):
        return httpx.Response(429, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_500_raises_source_unavailable():
    def handler(request):
        return httpx.Response(503, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_transport_error_raises_source_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler), max_retries=2, backoff_base=0)
    with pytest.raises(S.SourceUnavailable):
        src.fetch("sv4-123", "holofoil", CFG)


def test_pokemontcg_sends_api_key_header_when_env_set(monkeypatch):
    monkeypatch.setenv("POKEMONTCG_API_KEY", "secret-key")
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json=_CARD_JSON, request=request)

    src = S.PokemonTcgPriceSource(client=_client(handler))
    src.fetch("sv4-123", "holofoil", CFG)
    assert seen["key"] == "secret-key"


def test_collectr_stub_raises_not_configured():
    src = S.CollectrPriceSource()
    with pytest.raises(S.SourceNotConfigured):
        src.fetch("sv4-123", "holofoil", CFG)


def test_resolve_price_skips_unconfigured_uses_next():
    def ok_handler(request):
        return httpx.Response(200, json=_CARD_JSON, request=request)

    ordered = [S.CollectrPriceSource(), S.PokemonTcgPriceSource(client=_client(ok_handler))]
    cents, name = S.resolve_price(ordered, "sv4-123", "holofoil", CFG)
    assert (cents, name) == (1234, "pokemontcg")   # collectr skipped, pokemontcg used


def test_resolve_price_returns_genuine_miss_from_first_working_source():
    def miss_handler(request):
        return httpx.Response(200, json=_CARD_NO_HOLO, request=request)

    ordered = [S.PokemonTcgPriceSource(client=_client(miss_handler))]
    cents, name = S.resolve_price(ordered, "sv4-5", "holofoil", CFG)
    assert (cents, name) == (None, "pokemontcg")   # a returned None (no raise) wins immediately


def test_resolve_price_all_unavailable_raises():
    def down_handler(request):
        return httpx.Response(500, request=request)

    ordered = [
        S.CollectrPriceSource(),
        S.PokemonTcgPriceSource(client=_client(down_handler), max_retries=1, backoff_base=0),
    ]
    with pytest.raises(S.SourceUnavailable):
        S.resolve_price(ordered, "sv4-123", "holofoil", CFG)
```

- [ ] **Step 3: Run the sources tests to verify they fail**

Run: `cd worker && uv run pytest tests/test_pricing_sources.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.pricing.sources'`.

- [ ] **Step 4: Implement `sources.py`**

Create `worker/notbulk/pricing/sources.py`. Retry/backoff mirrors `download_refs._download_image`
(retry only 429/5xx/transport; exhausted retries -> `SourceUnavailable`), adapted to sync `httpx.Client`.

```python
"""Pluggable price sources.

A PriceSource maps (card_ref_id, finish) -> price in integer CENTS, or None for a
genuine miss (the card/finish simply has no market price). Transient failures
(429/5xx/transport) raise SourceUnavailable so the caller can try the next source
or, ultimately, let the price job retry via the queue's attempts/backoff.

Money is integer cents end to end (design §... / plan Global Constraints): a USD
float market price is rounded to the nearest cent exactly once, here.
"""
from __future__ import annotations

import os
import time
from typing import Protocol, runtime_checkable

import httpx

# tcgplayer price keys, verbatim — matches card_refs.finishes ordering used by the
# finish-spread rule (Task 8). Kept as the canonical finish vocabulary for M3.
FINISH_KEYS: tuple[str, ...] = ("normal", "holofoil", "reverseHolofoil")

_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BACKOFF_BASE = 1.0


class SourceUnavailable(Exception):
    """Transient source failure — try the next source, or retry the job later."""


class SourceNotConfigured(SourceUnavailable):
    """The source has no credentials/access yet (a permanent-for-now skip that is
    still treated as 'unavailable' so resolve_price moves on)."""


@runtime_checkable
class PriceSource(Protocol):
    name: str

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        """Return price in CENTS, or None for a genuine miss; raise
        SourceUnavailable on transient failure."""
        ...


def _round_to_cents(market_usd: float) -> int:
    return int(round(float(market_usd) * 100))


class PokemonTcgPriceSource:
    """GET {pokemontcg_base}/cards/{id}, read data.tcgplayer.prices[finish].market."""

    name = "pokemontcg"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
    ):
        # An injected client (tests hand in a MockTransport-backed one) is used
        # as-is; otherwise a real client is created lazily so import stays cheap.
        self._client = client
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_DEFAULT_TIMEOUT)
        return self._client

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        base = cfg["pricing"]["pokemontcg_base"].rstrip("/")
        url = f"{base}/cards/{card_ref_id}"
        headers: dict[str, str] = {}
        api_key = os.environ.get("POKEMONTCG_API_KEY")
        if api_key:
            headers["X-Api-Key"] = api_key   # raises the rate limit; pricing works keyless too

        client = self._get_client()
        delay = self._backoff_base
        for attempt in range(self._max_retries):
            try:
                resp = client.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._max_retries - 1:
                    raise SourceUnavailable(f"pokemontcg transport error: {exc}") from exc
                if delay:
                    time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries - 1:
                    raise SourceUnavailable(f"pokemontcg HTTP {resp.status_code}")
                if delay:
                    time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code >= 400:
                # A permanent 4xx (e.g. 404 for an unknown id) is a genuine miss,
                # not a transient failure — no price data for this card.
                return None

            data = (resp.json() or {}).get("data") or {}
            prices = ((data.get("tcgplayer") or {}).get("prices")) or {}
            entry = prices.get(finish)
            if not entry:
                return None
            market = entry.get("market")
            if market is None:
                return None
            return _round_to_cents(market)

        raise SourceUnavailable("pokemontcg retries exhausted")  # unreachable


class CollectrPriceSource:
    """Stub until COLLECTR_API_KEY is provisioned — always reports 'not configured'
    so resolve_price skips it (plan Global Constraints: Collectr is a stub for M3)."""

    name = "collectr"

    def fetch(self, card_ref_id: str, finish: str, cfg: dict) -> int | None:
        raise SourceNotConfigured("collectr access pending")


def resolve_price(
    sources: list[PriceSource], card_ref_id: str, finish: str, cfg: dict
) -> tuple[int | None, str]:
    """Try sources in order. The first source that RETURNS (int cents or None,
    without raising) wins -> (cents_or_None, source_name). A SourceUnavailable /
    SourceNotConfigured skips to the next source. If every source skips, re-raise
    the last SourceUnavailable so the caller can retry the job later."""
    last_exc: SourceUnavailable | None = None
    for src in sources:
        try:
            cents = src.fetch(card_ref_id, finish, cfg)
        except SourceUnavailable as exc:
            last_exc = exc
            continue
        return cents, src.name
    raise last_exc or SourceUnavailable("no price sources configured")
```

- [ ] **Step 5: Run the sources tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_pricing_sources.py -q`
Expected: PASS — 12 passed.

- [ ] **Step 6: Write the failing tests for `cache.py`**

Create `worker/tests/test_pricing_cache.py`. Uses the shared `FakePool` (which scripts one
row-list per `execute`). `read_cached` treats a NULL-`price_cents` row within TTL as a fresh
known-miss; the SQL's `WHERE fetched_at > now() - make_interval(hours => %s)` does the freshness
filter in-DB, so the fake returns a row only when the test intends "fresh".

```python
"""read_cached / upsert_price against FakePool. No real DB. Asserts both the
(fresh, cents) tuple and the SQL params bound."""
from __future__ import annotations

from notbulk.pricing import cache
from tests.fakes import FakePool


def test_read_cached_fresh_known_price():
    # A fresh row with a real price: the SELECT returns one row (price_cents, fetched_at).
    pool = FakePool([[(1234, "2026-07-16T00:00:00Z")]])
    fresh, cents = cache.read_cached(pool, "sv4-123", "holofoil", 24)
    assert (fresh, cents) == (True, 1234)
    sql, params = pool.cursor.executed[0]
    assert "from prices" in sql.lower()
    assert "make_interval" in sql.lower()
    # params: (card_ref_id, finish, ttl_hours) — order per the query
    assert params == ("sv4-123", "holofoil", 24)


def test_read_cached_fresh_null_known_miss():
    # A fresh row whose price_cents is NULL is a VALID known-miss: fresh=True, cents=None.
    pool = FakePool([[(None, "2026-07-16T00:00:00Z")]])
    fresh, cents = cache.read_cached(pool, "sv4-5", "holofoil", 24)
    assert (fresh, cents) == (True, None)


def test_read_cached_absent_or_stale_returns_not_fresh():
    # No row within TTL (absent OR older than TTL — the SQL filters stale out): empty result.
    pool = FakePool([[]])
    fresh, cents = cache.read_cached(pool, "sv4-999", "normal", 24)
    assert (fresh, cents) == (False, None)


def test_upsert_price_binds_all_columns():
    pool = FakePool([[]])   # upsert returns nothing
    cache.upsert_price(pool, "sv4-123", "holofoil", 1234, "pokemontcg")
    sql, params = pool.cursor.executed[0]
    low = sql.lower()
    assert "insert into prices" in low
    assert "on conflict (card_ref_id, finish) do update" in low
    assert params == ("sv4-123", "holofoil", 1234, "pokemontcg")


def test_upsert_price_stores_null_known_miss():
    pool = FakePool([[]])
    cache.upsert_price(pool, "sv4-5", "holofoil", None, "pokemontcg")
    _sql, params = pool.cursor.executed[0]
    assert params == ("sv4-5", "holofoil", None, "pokemontcg")   # NULL cents preserved
```

- [ ] **Step 7: Run the cache tests to verify they fail**

Run: `cd worker && uv run pytest tests/test_pricing_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.pricing.cache'`.

- [ ] **Step 8: Implement `cache.py`**

Create `worker/notbulk/pricing/cache.py`. Freshness is computed in SQL so a NULL price row
still counts as fresh (a cached known-miss must not be re-fetched within TTL).

```python
"""The prices cache: read_cached (with TTL freshness) and upsert_price.

A NULL price_cents row is a CACHED KNOWN-MISS — real "no data", not $0, and it
counts as fresh within the TTL so we don't re-hit the API for a card that simply
has no market price (plan Global Constraints / §5). Freshness is evaluated in the
query so a NULL price row is still 'fresh'.
"""
from __future__ import annotations

# Freshness filter in-DB: a row is fresh iff fetched_at is within ttl_hours of now.
# make_interval(hours => %s) keeps ttl_hours a bound parameter (never string-formatted).
_READ_SQL = (
    "SELECT price_cents, fetched_at FROM prices "
    "WHERE card_ref_id = %s AND finish = %s "
    "AND fetched_at > now() - make_interval(hours => %s)"
)

_UPSERT_SQL = (
    "INSERT INTO prices (card_ref_id, finish, price_cents, source, fetched_at) "
    "VALUES (%s, %s, %s, %s, now()) "
    "ON CONFLICT (card_ref_id, finish) DO UPDATE SET "
    "price_cents = EXCLUDED.price_cents, source = EXCLUDED.source, fetched_at = now()"
)


def read_cached(pool, card_ref_id: str, finish: str, ttl_hours: int) -> tuple[bool, int | None]:
    """Return (fresh, price_cents). fresh=True iff a row exists with fetched_at
    within ttl_hours (a NULL price_cents row is a valid fresh known-miss). A stale
    or absent row -> (False, None)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_READ_SQL, (card_ref_id, finish, ttl_hours))
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return False, None
    price_cents = row[0]   # may be None (known-miss); still a fresh hit
    return True, price_cents


def upsert_price(pool, card_ref_id: str, finish: str, price_cents: int | None, source: str) -> None:
    """Insert or refresh the cache row for (card_ref_id, finish). price_cents=None
    stores a known-miss; fetched_at is set to now() on both insert and update."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (card_ref_id, finish, price_cents, source))
        conn.commit()
```

- [ ] **Step 9: Run the cache tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_pricing_cache.py -q`
Expected: PASS — 5 passed.

- [ ] **Step 10: Run the full worker suite (no regressions)**

Run: `cd worker && uv run pytest tests ../eval/tests -q`
Expected: `196 passed, 4 skipped` (179 baseline + 12 sources + 5 cache).

- [ ] **Step 11: Commit**

```bash
git add worker/notbulk/pricing/__init__.py worker/notbulk/pricing/sources.py \
        worker/notbulk/pricing/cache.py \
        worker/tests/test_pricing_sources.py worker/tests/test_pricing_cache.py
git commit -m "feat(worker): pluggable price sources and price cache"
```

---

### Task 3: `handlers/price.py` (the `price` job handler)

**Files:**
- Create: `worker/notbulk/handlers/price.py`
- Modify: `worker/notbulk/worker.py:37-43` (register `'price'` in `_build_handlers`)
- Modify: `worker/notbulk/jobqueue.py:18-23` (add `'price'` to `_REQUIRED_KEYS`)
- Test: `worker/tests/test_handler_price.py`

**Interfaces:**
- Consumes:
  - `cache.read_cached(pool, card_ref_id, finish, ttl_hours) -> (bool, int | None)` (Task 2)
  - `cache.upsert_price(pool, card_ref_id, finish, price_cents, source) -> None` (Task 2)
  - `resolve_price(sources, card_ref_id, finish, cfg) -> (int | None, str)`, `SourceUnavailable`,
    `PokemonTcgPriceSource`, `CollectrPriceSource` (Task 2)
  - `finish.maybe_narrow_finish_flag(pool, card_ref_id, cfg) -> None` (Task 8 — imported here;
    this task's tests monkeypatch it to a no-op so they don't depend on Task 8)
  - `jobqueue.validate_payload` `_REQUIRED_KEYS` pattern (`worker/notbulk/jobqueue.py`)
- Produces: `handle_price(pool, storage, payload: dict, cfg: dict) -> None` (the handler dispatched
  by `worker.py` for the `'price'` job type).

- [ ] **Step 1: Add the `price` payload contract to `jobqueue._REQUIRED_KEYS`**

Modify `worker/notbulk/jobqueue.py` — add the `'price'` entry to `_REQUIRED_KEYS` (both
`card_ref_id` and `finish` are plain string keys, so the default string-check in
`validate_payload` covers them; no special-casing like `predicted_ref_id` is needed):

```python
_REQUIRED_KEYS: dict[str, set[str]] = {
    "detect": {"photo_id"},
    "identify": {"card_id"},
    "fetch_source": {"photo_id"},
    "ingest_correction": {"card_id", "actual_ref_id", "predicted_ref_id"},
    "price": {"card_ref_id", "finish"},
}
```

(This coordinates with Task 1's `jobs.type` CHECK, which now admits `'price'`. Task 4 also
touches this line; the edit is idempotent — if `'price'` is already present, leave it.)

- [ ] **Step 2: Register the handler in `worker.py`**

Modify `worker/notbulk/worker.py`. Add the import near the other handler imports:

```python
from .handlers import price as price_handler
```

and add the one-line entry to `_build_handlers`'s dict:

```python
def _build_handlers():
    return {
        "detect": detect_handler.handle_detect,
        "identify": identify_handler.handle_identify,
        "fetch_source": fetch_handler.handle_fetch,
        "ingest_correction": handle_ingest_correction,
        "price": price_handler.handle_price,
    }
```

- [ ] **Step 3: Write the failing tests for `handle_price`**

Create `worker/tests/test_handler_price.py`. `resolve_price`, the cache helpers, and
`finish.maybe_narrow_finish_flag` are monkeypatched so no network/DB/Task-8 dependency exists.

```python
"""handle_price: cache-fresh short-circuit, resolve+upsert, NULL known-miss,
SourceUnavailable re-raise, and the post-upsert narrow call. resolve_price /
cache / finish.maybe_narrow_finish_flag are all monkeypatched — no network, no
DB, no Task-8 dependency."""
from __future__ import annotations

import pytest

from notbulk.handlers import price as ph
from notbulk.pricing.sources import SourceUnavailable

CFG = {
    "pricing": {
        "source_order": ["pokemontcg"],
        "cache_ttl_hours": 24,
        "pokemontcg_base": "https://api.pokemontcg.io/v2",
    }
}
PAYLOAD = {"card_ref_id": "sv4-123", "finish": "holofoil"}


class _Spy:
    """Captures calls so a test can assert what the handler did."""
    def __init__(self):
        self.upserts = []
        self.narrowed = []
        self.resolved = []


def _wire(monkeypatch, spy, *, fresh, cached_cents=None, resolve_result=None, resolve_raises=None):
    monkeypatch.setattr(ph.cache, "read_cached",
                        lambda pool, cr, fin, ttl: (fresh, cached_cents))

    def _resolve(sources, cr, fin, cfg):
        spy.resolved.append((cr, fin))
        if resolve_raises is not None:
            raise resolve_raises
        return resolve_result

    monkeypatch.setattr(ph, "resolve_price", _resolve)
    monkeypatch.setattr(ph.cache, "upsert_price",
                        lambda pool, cr, fin, cents, source: spy.upserts.append((cr, fin, cents, source)))
    monkeypatch.setattr(ph.finish, "maybe_narrow_finish_flag",
                        lambda pool, cr, cfg: spy.narrowed.append(cr))


def test_fresh_cache_is_a_noop():
    spy = _Spy()
    import pytest as _pt
    with _pt.MonkeyPatch.context() as mp:
        _wire(mp, spy, fresh=True, cached_cents=1234)
        ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.resolved == []      # resolve NOT called on a fresh hit
    assert spy.upserts == []       # nothing re-written
    assert spy.narrowed == []      # no narrow on a no-op


def test_resolves_and_upserts_then_narrows(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(1234, "pokemontcg"))
    ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.resolved == [("sv4-123", "holofoil")]
    assert spy.upserts == [("sv4-123", "holofoil", 1234, "pokemontcg")]
    assert spy.narrowed == ["sv4-123"]   # narrow invoked AFTER a successful upsert


def test_genuine_miss_upserts_null(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(None, "pokemontcg"))
    ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.upserts == [("sv4-123", "holofoil", None, "pokemontcg")]   # NULL known-miss cached
    assert spy.narrowed == ["sv4-123"]   # a cached miss still triggers narrow evaluation


def test_source_unavailable_reraises_and_does_not_upsert(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_raises=SourceUnavailable("down"))
    with pytest.raises(SourceUnavailable):
        ph.handle_price(pool=object(), storage=object(), payload=PAYLOAD, cfg=CFG)
    assert spy.upserts == []        # no cache write on transient failure
    assert spy.narrowed == []       # and no narrow


def test_missing_payload_key_raises_value_error(monkeypatch):
    spy = _Spy()
    _wire(monkeypatch, spy, fresh=False, resolve_result=(1234, "pokemontcg"))
    with pytest.raises(ValueError):
        ph.handle_price(pool=object(), storage=object(), payload={"card_ref_id": "sv4-123"}, cfg=CFG)
```

- [ ] **Step 4: Run the handler tests to verify they fail**

Run: `cd worker && uv run pytest tests/test_handler_price.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.handlers.price'`.

- [ ] **Step 5: Implement `handlers/price.py`**

Create `worker/notbulk/handlers/price.py`. The configured source classes are built from
`cfg.pricing.source_order` via a name->class registry. `finish` is imported from
`handlers.finish` (Task 8 writes `maybe_narrow_finish_flag`); this handler only calls it.

```python
"""price job handler.

payload {card_ref_id, finish}:
  read_cached -> if fresh (incl. a fresh NULL known-miss), no-op and return.
  else: build the configured source list (cfg.pricing.source_order), resolve_price,
        upsert (a genuine miss stores price_cents=NULL so we don't re-hit within TTL).
        SourceUnavailable from resolve_price PROPAGATES so the job retries via the
        queue's attempts/backoff (worker.py's fail(dead=False)).
  after a successful upsert, invoke finish.maybe_narrow_finish_flag (Task 8) so a
  now-priced finish can clear a finish_needs_confirmation flag when the spread is small.

Money is integer cents throughout (plan Global Constraints).
"""
from __future__ import annotations

from .. import jobqueue
from ..pricing import cache
from ..pricing.sources import (CollectrPriceSource, PokemonTcgPriceSource,
                               resolve_price)
from . import finish

# name -> PriceSource class. cfg.pricing.source_order lists the names to try, in order.
_SOURCE_REGISTRY = {
    "pokemontcg": PokemonTcgPriceSource,
    "collectr": CollectrPriceSource,
}


def _build_sources(cfg: dict) -> list:
    order = cfg["pricing"]["source_order"]
    return [_SOURCE_REGISTRY[name]() for name in order]


def handle_price(pool, storage, payload: dict, cfg: dict) -> None:
    # Payload shape is enforced by jobqueue.validate_payload at dispatch, but this
    # keeps the handler safe if called directly (tests) — same defensive read as peers.
    jobqueue.validate_payload("price", payload)
    card_ref_id = payload["card_ref_id"]
    finish_key = payload["finish"]
    ttl_hours = int(cfg["pricing"]["cache_ttl_hours"])

    fresh, _cents = cache.read_cached(pool, card_ref_id, finish_key, ttl_hours)
    if fresh:
        # A fresh row (real price OR a fresh NULL known-miss) needs no refetch.
        return

    # resolve_price raises SourceUnavailable if every source skips — let it
    # propagate so the queue retries the job (do NOT cache a transient failure).
    price_cents, source_name = resolve_price(
        _build_sources(cfg), card_ref_id, finish_key, cfg
    )
    # A genuine miss (price_cents is None) is cached as a known-miss so we don't
    # re-hit the API within the TTL.
    cache.upsert_price(pool, card_ref_id, finish_key, price_cents, source_name)

    # Now that this finish is priced, a card deferred purely on finish spread may
    # be narrowable (Task 8's strict guard decides; never a wrong auto-accept).
    finish.maybe_narrow_finish_flag(pool, card_ref_id, cfg)
```

- [ ] **Step 6: Provide a minimal `handlers/finish` import target**

`handlers/price.py` does `from . import finish`, which imports `worker/notbulk/handlers/finish.py`.
Task 8 writes the real `maybe_narrow_finish_flag`; this task's tests monkeypatch it, but the
module must EXIST and expose the name for the import + monkeypatch to resolve. Create a stub
`worker/notbulk/handlers/finish.py` that Task 8 will replace/expand:

```python
"""finish-spread narrowing (invoked by handle_price after a finish is priced).

STUB placeholder created in Task 3 so handle_price can import it; the real strict
guarded implementation lands in Task 8. Until then this is a safe no-op — a
no-op here can never cause a wrong auto-accept (it simply leaves the card in
validation), preserving the hard invariant.
"""
from __future__ import annotations


def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None:
    """No-op stub (Task 8 replaces this with the guarded narrowing logic)."""
    return None
```

- [ ] **Step 7: Run the handler tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_handler_price.py -q`
Expected: PASS — 5 passed.

- [ ] **Step 8: Run the full worker suite (handler registration + validate_payload)**

Run: `cd worker && uv run pytest tests ../eval/tests -q`
Expected: `201 passed, 4 skipped` (196 from Task 2 + 5 new handler tests). No existing
`test_jobqueue.py` / `test_worker`-style test breaks from the added `_REQUIRED_KEYS` entry
or the handler dict addition.

- [ ] **Step 9: Commit**

```bash
git add worker/notbulk/handlers/price.py worker/notbulk/handlers/finish.py \
        worker/notbulk/worker.py worker/notbulk/jobqueue.py \
        worker/tests/test_handler_price.py
git commit -m "feat(worker): price job handler"
```

---

### Task 4: Enqueue `price` jobs when a card resolves (modify `identify`)

**Files:**
- Modify: `worker/notbulk/handlers/identify.py:161-174` (the `if ident.card_ref_id is not None:`
  finish block — reuse the finishes it already SELECTs, then enqueue one `price` job per finish)
- Modify: `worker/notbulk/jobqueue.py:18-23` (ensure `'price'` is in `_REQUIRED_KEYS` — idempotent
  with Task 3)
- Test: `worker/tests/test_handler_identify.py` (extend)

**Interfaces:**
- Consumes:
  - `jobqueue.enqueue(pool, job_type, payload, *, batch_id=None, user_id=None) -> str`
    (`worker/notbulk/jobqueue.py:187`)
  - `jobqueue.validate_payload` `_REQUIRED_KEYS['price'] = {'card_ref_id','finish'}` (Task 3;
    re-asserted idempotently here)
  - the existing `_SELECT_FINISHES_SQL = "SELECT finishes FROM card_refs WHERE id = %s"` and the
    `finishes` list already read in `handle_identify` (`identify.py:49, 161-174`)
- Produces: no new symbol — a behavioral extension of `handle_identify`. After a card resolves
  with a non-null `card_ref_id`, one `price` job per finish in `card_refs.finishes` is enqueued.

- [ ] **Step 1: Ensure `jobqueue._REQUIRED_KEYS` accepts `'price'` (idempotent)**

Confirm `worker/notbulk/jobqueue.py`'s `_REQUIRED_KEYS` contains the `'price'` entry (added in
Task 3). If Task 3 has not landed in this working tree, add it now — the change is identical and
idempotent:

```python
_REQUIRED_KEYS: dict[str, set[str]] = {
    "detect": {"photo_id"},
    "identify": {"card_id"},
    "fetch_source": {"photo_id"},
    "ingest_correction": {"card_id", "actual_ref_id", "predicted_ref_id"},
    "price": {"card_ref_id", "finish"},
}
```

- [ ] **Step 2: Write the failing tests (extend `test_handler_identify.py`)**

Append to `worker/tests/test_handler_identify.py`. The `_patch` helper monkeypatches
`identify_crop` etc.; extend it in the new tests to also capture `jobqueue.enqueue` calls.
A card with `finishes=['normal','holofoil']` must enqueue EXACTLY two `price` jobs (one per
finish) with `{'card_ref_id': <ref>, 'finish': <f>}`; a null-`card_ref_id` card enqueues zero.

```python
def _patch_with_enqueue(monkeypatch, ident):
    """Like _patch, but also captures jobqueue.enqueue(...) calls."""
    notified = _patch(monkeypatch, ident)
    enqueued = []
    monkeypatch.setattr(
        ih.jobqueue, "enqueue",
        lambda pool, jtype, payload, **kw: enqueued.append((jtype, payload, kw)) or "job-id",
    )
    return notified, enqueued


def _price_jobs(enqueued):
    return [(payload, kw) for jtype, payload, kw in enqueued if jtype == "price"]


def test_resolved_card_enqueues_one_price_job_per_finish(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal", "holofoil"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)

    prices = _price_jobs(enqueued)
    payloads = sorted(p["finish"] for p, _kw in prices)
    assert payloads == ["holofoil", "normal"]                 # exactly one per finish
    assert all(p["card_ref_id"] == "sv4-1" for p, _kw in prices)
    # enqueued with batch/user context so the price jobs stay attributable
    assert all(kw.get("batch_id") == "batch-1" and kw.get("user_id") == "user-1"
               for _p, kw in prices)


def test_single_finish_card_enqueues_one_price_job(monkeypatch):
    ident = _ident("sv4-1", "h", [MethodResult("h", "sv4-1", 0.9)], ["sv4-1"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=["normal"]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    prices = _price_jobs(enqueued)
    assert [p["finish"] for p, _kw in prices] == ["normal"]


def test_null_card_ref_id_enqueues_no_price_jobs(monkeypatch):
    ident = _ident(None, "validation", [MethodResult("a", "sv4-2", 0.5)], ["sv4-2"])
    _notified, enqueued = _patch_with_enqueue(monkeypatch, ident)
    pool = ScriptedPool(_responder(finishes=[]))
    ih.handle_identify(pool, FakeStorage(), {"card_id": "card-1"}, CFG)
    assert _price_jobs(enqueued) == []                        # no ref id -> no price jobs
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `cd worker && uv run pytest tests/test_handler_identify.py -q -k "price_job or price_jobs"`
Expected: FAIL — the three new tests fail (`handle_identify` does not yet enqueue `price` jobs;
`_price_jobs(enqueued)` is empty).

- [ ] **Step 4: Modify `handle_identify` to enqueue price jobs**

In `worker/notbulk/handlers/identify.py`, the `if ident.card_ref_id is not None:` block already
reads `finishes` for the A1 finish gating. Reuse that `finishes` list — after the card UPDATE +
`notify_progress`, enqueue one `price` job per finish. Enqueuing after the UPDATE (not inside the
finish block) keeps the price fan-out off the identity-write path and matches detect.py's
"persist, then enqueue downstream" ordering.

Change the finish block to keep `finishes` in scope for reuse (it already is — `finishes` is
assigned at `identify.py:167`), then add the enqueue loop AFTER the existing
`jobqueue.notify_progress(pool, batch_id, "card_identified", ...)` call:

```python
    jobqueue.notify_progress(pool, batch_id, "card_identified", card_id=card_id)

    # Fan out one price job per finish of the resolved card. A card with no
    # card_ref_id (validation/unreadable/null id) gets NO price jobs. `finishes`
    # is the same list read above for the A1 finish rule; a card has few finishes
    # so iterating the list is the de-dup (at most one job per (card_ref_id, finish)).
    if ident.card_ref_id is not None:
        for finish_key in finishes:
            jobqueue.enqueue(
                pool, "price",
                {"card_ref_id": ident.card_ref_id, "finish": finish_key},
                batch_id=batch_id, user_id=user_id,
            )
```

Note: `finishes` is defined inside `if ident.card_ref_id is not None:` at the top of the handler
(`identify.py:167`). Because the enqueue loop is itself guarded by the same
`if ident.card_ref_id is not None:`, `finishes` is guaranteed bound whenever the loop runs. If a
linter flags possible-unbound, initialize `finishes: list[str] = []` alongside the
`finish`/`finish_needs_confirmation` defaults at `identify.py:158-159` and drop the redundant
inner guard — either form satisfies the tests.

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd worker && uv run pytest tests/test_handler_identify.py -q -k "price_job or price_jobs"`
Expected: PASS — 3 passed.

- [ ] **Step 6: Run the whole identify test file (no regressions)**

Run: `cd worker && uv run pytest tests/test_handler_identify.py -q`
Expected: PASS — all prior identify tests (status mapping, A1 downgrade, candidates, llm_calls,
single-fire batch completion, nonexistent-card no-op) still pass, plus the 3 new ones.

Note: the pre-existing tests do NOT patch `jobqueue.enqueue`. For the resolved-card cases
(`sv4-1`), the real `jobqueue.enqueue` would run against the `ScriptedPool` and call
`validate_payload("price", ...)` — which now passes (Step 1) — then execute the INSERT + NOTIFY
against the scripted cursor (returns `[]`, harmless). Confirm those tests are unaffected; if any
pre-existing resolved-card test asserts on `pool.cursor.executed` length, it may see the extra
enqueue INSERT/NOTIFY rows — in that case, switch that test to `_patch_with_enqueue` so enqueue is
captured rather than executed. (The current assertions only inspect the `UPDATE cards` row and
`notified`, so they are unaffected.)

- [ ] **Step 7: Run the full worker + eval suite**

Run: `cd worker && uv run pytest tests ../eval/tests -q`
Expected: `204 passed, 4 skipped` (201 from Task 3 + 3 new identify tests).

- [ ] **Step 8: Commit**

```bash
git add worker/notbulk/handlers/identify.py worker/notbulk/jobqueue.py \
        worker/tests/test_handler_identify.py
git commit -m "feat(worker): enqueue price jobs when a card resolves"
```
<!--
M3 plan — Part 2 (web layer: reference-image proxy, collection explorer, CSV export).
Assembled into docs/superpowers/plans/2026-07-16-m3-pricing-explorer.md.
The plan header (Global Constraints, File Structure, Interface Contract) in that file is
authoritative and its constraints implicitly apply to every task below.
-->

### Task 5: Reference-image proxy (`refproxy.ts` + `refimg.ts` route)

**Files:**
- Create: `web/src/services/refproxy.ts`
- Modify: `web/src/routes/images.ts` (add the `/img/ref/:cardRefId` route to the EXISTING `imagesRouter`)
- Modify: `web/src/config.ts` (add the `refproxy` key to the `Config` type)
- Test: `web/tests/refproxy.test.ts` (unit — `ensureRefCached`)
- Test: `web/tests/refimg.route.test.ts` (route — 302 / 404)

**Interfaces:**
- Consumes:
  - `Storage` from `web/src/services/storage.ts` — `put(key, body, contentType)`, `signedGetUrl(key)`.
  - `Pool` from `pg` — `pool.query(sql, params)`.
  - `Config` from `web/src/config.ts` — the new `refproxy` block.
  - `imagesRouter(pool, storage, cfg)` from `web/src/routes/images.ts` (already mounted in `app.ts` behind `app.use("/img", requireUser())`).
- Produces:
  - `export async function ensureRefCached(pool: Pool, storage: Storage, cfg: Config, cardRefId: string): Promise<string | null>` — returns the MinIO key `${cfg.refproxy.cache_prefix}/${cardRefId}.webp`, caching on first call; `null` on any failure or a non-allowlisted host.
  - Route `GET /img/ref/:cardRefId` (already `requireUser`-gated by the app-level `/img` mount) → 302 to `storage.signedGetUrl(key)`, or 404 when `ensureRefCached` returns `null`.

**Security note (from the plan's Global Constraints — state this in the code comment):** this proxy is
**NOT** the SSRF surface. It fetches ONLY `card_refs.image_url` — trusted reference data populated from
pokemontcg.io at index-build time, never user input — and only when the parsed hostname is exactly
`cfg.refproxy.allowed_image_host` (`"images.pokemontcg.io"`). The user-URL fetcher with the full SSRF gate
is M2's worker-side code. A WHATWG-`URL`-parse plus an exact single-host equality check is the complete and
correct control here; no allow-list-of-many, no DNS-rebind defense, no IP-literal handling is required
because the input domain is fixed reference data.

**Mounting decision:** `app.ts` already has `app.use("/img", requireUser())` immediately before
`app.use(imagesRouter(pool, storage))`, so anything under `/img` is authenticated. Add the ref route to the
existing `imagesRouter` (it is already `/img`-scoped and `requireUser`-gated) rather than creating a new
router — cleaner, no new `app.ts` mount. `app.ts` therefore does **not** change in this task.

**Config note:** the contract's key is built inline as `${cfg.refproxy.cache_prefix}/${cardRefId}.webp`. Do
**not** add a `Storage.refKey` helper — reference art is global (no `userId`/`batchId`), unlike `photoKey`/
`cropKey`, so an inline key in `refproxy.ts` keeps `Storage` owner-scoped and avoids a one-off method.

- [ ] **Step 1: Add the `refproxy` config type**

In `web/src/config.ts`, add to the `Config` interface (after the `turnstile` field):

```ts
  turnstile: { site_key: string; secret: string };
  refproxy: { allowed_image_host: string; cache_prefix: string; max_bytes: number };
}
```

- [ ] **Step 2: Extend the test `Config` stub with a `refproxy` block**

In `web/tests/helpers.ts`, add a `refproxy` block to `testCfg` (after the `turnstile` block, before the closing `}`):

```ts
  turnstile: { site_key: "test-site-key", secret: "test-secret" },
  refproxy: { allowed_image_host: "images.pokemontcg.io", cache_prefix: "refs", max_bytes: 5242880 },
} as unknown as Config;
```

- [ ] **Step 3: Write the failing `ensureRefCached` unit tests**

Create `web/tests/refproxy.test.ts`:

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import sharp from "sharp";
import { ensureRefCached } from "../src/services/refproxy.js";
import { FakePool, FakeStorage, testCfg } from "./helpers.js";
import type { Config } from "../src/config.js";
import type { Pool } from "pg";
import type { Storage } from "../src/services/storage.js";

// A tiny real PNG so sharp() has something valid to transcode on the first-fetch path.
async function tinyPng(): Promise<Buffer> {
  return sharp({
    create: { width: 4, height: 4, channels: 3, background: { r: 10, g: 20, b: 30 } },
  })
    .png()
    .toBuffer();
}

// Build a Response-like object for the mocked global fetch: ok, headers.get, arrayBuffer().
function fetchResponse(body: Buffer, contentType = "image/png", ok = true, status = 200) {
  return {
    ok,
    status,
    headers: { get: (h: string) => (h.toLowerCase() === "content-type" ? contentType : h.toLowerCase() === "content-length" ? String(body.length) : null) },
    arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength),
  } as unknown as Response;
}

afterEach(() => vi.restoreAllMocks());

describe("ensureRefCached", () => {
  it("returns null for a null/empty cardRefId without touching the pool", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "");
    expect(key).toBeNull();
    expect(pool.calls.length).toBe(0);
  });

  it("cached-key path: returns the existing ref_cached_key, never fetches", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: "refs/ref-1.webp", image_url: "https://images.pokemontcg.io/x/1.png" }] });
    const storage = new FakeStorage();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBe("refs/ref-1.webp");
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(storage.puts.length).toBe(0);
  });

  it("first-fetch path: fetches, transcodes to webp, puts, updates card_refs, returns key", async () => {
    const png = await tinyPng();
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] }); // SELECT
    pool.enqueue({ rows: [{ id: "ref-1" }] }); // UPDATE ... RETURNING
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(fetchResponse(png, "image/png"));

    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");

    expect(key).toBe("refs/ref-1.webp");
    // put'd a webp under the inline refs/{id}.webp key
    expect(storage.puts.length).toBe(1);
    expect(storage.puts[0].key).toBe("refs/ref-1.webp");
    expect(storage.puts[0].contentType).toBe("image/webp");
    expect(storage.puts[0].body.slice(0, 4).toString("latin1")).toBe("RIFF"); // WEBP container magic
    // card_refs updated with the cached key
    const update = pool.calls[1];
    expect(update.sql).toMatch(/UPDATE card_refs SET ref_cached_key/i);
    expect(update.params).toEqual(["refs/ref-1.webp", "ref-1"]);
  });

  it("non-allowlisted image_url host: returns null, never fetches", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://evil.example.com/x.png" }] });
    const storage = new FakeStorage();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(storage.puts.length).toBe(0);
  });

  it("fetch failure (non-ok / transport throw): returns null, no put, no update", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("boom"));
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0);
    expect(pool.calls.length).toBe(1); // only the SELECT ran
  });

  it("non-image content-type: returns null, no put", async () => {
    const png = await tinyPng();
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(fetchResponse(png, "text/html"));
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0);
  });
});
```

- [ ] **Step 4: Run the unit tests to verify they fail**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/refproxy.test.ts
```

Expected: FAIL — `Failed to resolve import "../src/services/refproxy.js"` (module does not exist yet).

- [ ] **Step 5: Implement `refproxy.ts`**

Create `web/src/services/refproxy.ts`:

```ts
import sharp from "sharp";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { Storage } from "./storage.js";

/**
 * Reference-image proxy. NOT an SSRF surface: it fetches ONLY card_refs.image_url —
 * trusted reference data populated from pokemontcg.io at index-build time, never user
 * input — and only when the parsed hostname is EXACTLY cfg.refproxy.allowed_image_host
 * ("images.pokemontcg.io"). The user-URL fetcher with the full SSRF gate is M2's
 * worker-side code. A WHATWG-URL parse + exact single-host equality check is the complete
 * control here. On ANY failure the function returns null and the caller renders a placeholder.
 */
export async function ensureRefCached(
  pool: Pool,
  storage: Storage,
  cfg: Config,
  cardRefId: string,
): Promise<string | null> {
  if (!cardRefId) return null;
  try {
    const { rows } = await pool.query(
      `SELECT ref_cached_key, image_url FROM card_refs WHERE id = $1`,
      [cardRefId],
    );
    const ref = rows[0];
    if (!ref) return null;
    if (ref.ref_cached_key) return ref.ref_cached_key as string;
    if (!ref.image_url) return null;

    // WHATWG parse + exact-host check (the complete control — see header comment).
    let url: URL;
    try {
      url = new URL(ref.image_url as string);
    } catch {
      return null;
    }
    if (url.protocol !== "https:") return null;
    if (url.hostname !== cfg.refproxy.allowed_image_host) return null;

    // redirect:"error" — a redirect off the pinned host would defeat the host check.
    const resp = await fetch(url, { redirect: "error" });
    if (!resp.ok) return null;

    const ctype = resp.headers.get("content-type") ?? "";
    if (!ctype.startsWith("image/")) return null;

    // Content-Length cap (best-effort; the arrayBuffer read below is the hard bound).
    const declared = Number(resp.headers.get("content-length") ?? "0");
    if (declared && declared > cfg.refproxy.max_bytes) return null;

    const raw = Buffer.from(await resp.arrayBuffer());
    if (raw.byteLength > cfg.refproxy.max_bytes) return null;

    const webp = await sharp(raw).webp().toBuffer();
    const key = `${cfg.refproxy.cache_prefix}/${cardRefId}.webp`;
    await storage.put(key, webp, "image/webp");
    await pool.query(
      `UPDATE card_refs SET ref_cached_key = $1 WHERE id = $2 RETURNING id`,
      [key, cardRefId],
    );
    return key;
  } catch {
    return null;
  }
}
```

- [ ] **Step 6: Run the unit tests to verify they pass**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/refproxy.test.ts
```

Expected: PASS (6 passed).

- [ ] **Step 7: Write the failing route test**

Create `web/tests/refimg.route.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { FakePool, FakeStorage, makeDeps } from "./helpers.js";

// Stub session middleware: force a fixed authed user (mirrors images.test.ts).
const AUTHED_USER = { id: "u1", email: "a@b.com", tier: "free" };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

describe("GET /img/ref/:cardRefId", () => {
  it("302 → signed URL when the ref image is already cached", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: "refs/ref-1.webp", image_url: "https://images.pokemontcg.io/x/1.png" }] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get("/img/ref/ref-1");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("http://127.0.0.1:9000/notbulk/refs/ref-1.webp?sig=canned");
  });

  it("404 when ensureRefCached returns null (card_ref missing)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get("/img/ref/missing");
    expect(res.status).toBe(404);
  });

  it("302 redirects anonymous users to / (requireUser gate on /img)", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    // Default session seam + no cookie → req.user unset → requireUser 302 → "/".
    const app = createApp(makeDeps({ pool, storage: storage as any }));
    const res = await request(app).get("/img/ref/ref-1");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
```

- [ ] **Step 8: Run the route test to verify it fails**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/refimg.route.test.ts
```

Expected: FAIL — the first two cases 404 (route not registered) instead of 302/404-as-designed; the imports resolve but `GET /img/ref/:cardRefId` falls through to `notFound()`.

- [ ] **Step 9: Add the ref route to the existing `imagesRouter`**

Modify `web/src/routes/images.ts`. Update the imports and signature to thread `cfg`, and add the ref route:

```ts
import { Router } from "express";
import type { Pool } from "pg";
import type { AuthedRequest } from "../middleware/session.js";
import type { Storage } from "../services/storage.js";
import type { Config } from "../config.js";
import { getOwnedPhoto } from "../queries/batches.js";
import { getOwnedCardCrop } from "../queries/cards.js";
import { ensureRefCached } from "../services/refproxy.js";

export function imagesRouter(pool: Pool, storage: Storage, cfg: Config): Router {
  const r = Router();

  // 404 (not 403) when not owned — ownership failure is indistinguishable
  // from a missing object (AC 7: no route reveals another user's ids).
  r.get("/img/photo/:id", async (req: AuthedRequest, res) => {
    const id = req.params.id as string;
    const photo = await getOwnedPhoto(pool, req.user!.id, id);
    if (!photo || !photo.storage_key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(photo.storage_key);
    return res.redirect(302, url);
  });

  r.get("/img/crop/:id", async (req: AuthedRequest, res) => {
    const id = req.params.id as string;
    const card = await getOwnedCardCrop(pool, req.user!.id, id);
    if (!card || !card.crop_storage_key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(card.crop_storage_key);
    return res.redirect(302, url);
  });

  // Reference art is GLOBAL, not owner-scoped (unlike /img/crop): card_refs is shared
  // reference data. requireUser (applied by the app-level /img mount) gates access to
  // authenticated users, but there is no per-user ownership check on reference images.
  r.get("/img/ref/:cardRefId", async (req: AuthedRequest, res) => {
    const key = await ensureRefCached(pool, storage, cfg, req.params.cardRefId as string);
    if (!key) return res.sendStatus(404);
    const url = await storage.signedGetUrl(key);
    return res.redirect(302, url);
  });

  return r;
}
```

- [ ] **Step 10: Update the `imagesRouter` mount in `app.ts` to pass `cfg`**

Modify `web/src/app.ts:82`. Change:

```ts
  app.use(imagesRouter(pool, storage));
```

to:

```ts
  app.use(imagesRouter(pool, storage, cfg));
```

- [ ] **Step 11: Run the route test to verify it passes**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/refimg.route.test.ts
```

Expected: PASS (3 passed).

- [ ] **Step 12: Run the full web suite (guards the `imagesRouter` signature change)**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run
```

Expected: PASS — existing `images.test.ts` still passes because `makeDeps` supplies `cfg: testCfg` and `app.ts` now forwards it.

- [ ] **Step 13: Commit**

```bash
git add web/src/services/refproxy.ts web/src/routes/images.ts web/src/app.ts web/src/config.ts \
        web/tests/refproxy.test.ts web/tests/refimg.route.test.ts web/tests/helpers.ts
git commit -m "feat(web): reference-image proxy with local MinIO cache"
```

---

### Task 6: Collection explorer (`queries/collection.ts` + `routes/collection.ts` GET /collection + `collection.njk` + `collection.js`)

**Files:**
- Create: `web/src/lib/money.ts` (shared `formatCents` — also consumed by Task 7's CSV export)
- Create: `web/src/queries/collection.ts`
- Create: `web/src/routes/collection.ts`
- Create: `web/views/collection.njk`
- Create: `web/public/js/collection.js`
- Modify: `web/src/app.ts` (mount `collectionRouter`)
- Test: `web/tests/money.test.ts` (unit — `formatCents`, incl. whole-dollar/trailing-zero cases)
- Test: `web/tests/collection.query.test.ts` (unit — SQL shape/params, stats, injection-safety)
- Test: `web/tests/collection.route.test.ts` (route — grid + stats render, money formatting, ownership)

**Interfaces:**
- Consumes: `Pool` (`pool.query`), `requireUser` from `web/src/middleware/session.ts`, `AuthedRequest`.
- Produces (VERBATIM from the plan's Interface Contract):

```ts
export interface CollectionRow {
  card_id: string; card_ref_id: string | null; crop_storage_key: string | null;
  name: string | null; set_name: string | null; number: string | null;
  finish: string | null; quantity: number; confidence: number;
  status: string;                        // auto | validated | corrected (explorer shows these only)
  price_cents: number | null; price_source: string | null; price_fetched_at: string | null;
  batch_id: string;
  has_price_row: boolean;                // (pr.card_ref_id IS NOT NULL) — distinguishes "pending price" (no row) from "no price data" (row, price_cents NULL)
}
export interface CollectionStats { total_cards: number; total_value_cents: number; priced_fraction: number; oldest_price_at: string | null }
export async function getCollection(pool: Pool, userId: string, opts: {
  batchId?: string; set?: string; finish?: string; source?: 'auto'|'corrected'; sort: string; limit: number; offset: number;
}): Promise<CollectionRow[]>;
export async function getCollectionStats(pool: Pool, userId: string, opts: {
  batchId?: string; set?: string; finish?: string; source?: 'auto'|'corrected';
}): Promise<CollectionStats>;
```

- Route `GET /collection` (`requireUser`, owner-scoped) → renders `collection.njk` with `{ rows, stats, filters, page, pageSize, hasNext }`.

**Design notes:**
- `has_price_row` extends the contract's `CollectionRow` — the contract's §6.6 amendment #3 requires distinguishing "pending price" (LEFT JOIN produced no `prices` row) from "no price data" (a row exists with `price_cents` NULL). Implemented as `(pr.card_ref_id IS NOT NULL) AS has_price_row` in the SELECT.
- **Money stays integer cents** in the query and interfaces (`CollectionRow.price_cents`, `CollectionStats.total_value_cents`). Formatting to `$X.XX` happens in the ROUTE (`routes/collection.ts`), via the shared `formatCents(cents: number): string` helper from `web/src/lib/money.ts` — the SAME helper Task 7's CSV export uses (`(cents/100).toFixed(2)` with a `$` prefix; no divergent money formatters). The route attaches display strings — `row.price_display` (`'pending price'` when `!has_price_row`, `'no price data'` when `has_price_row && price_cents == null`, else `formatCents(price_cents)`) and `stats.total_value_display` (`formatCents(stats.total_value_cents)`) — and `collection.njk` renders those strings with **no arithmetic in the template**. `getCollectionStats` returns `total_value_cents` (integer), `priced_fraction` (float 0..1).
- **Parameterize everything.** The only non-parameter dynamic SQL is the ORDER BY, chosen from a hardcoded whitelist switch (never interpolated user input) and the filter fragments (each fragment is fixed text; only its bound `$n` value varies).

- [ ] **Step 1: Write the failing query tests**

Create `web/tests/collection.query.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { getCollection, getCollectionStats } from "../src/queries/collection.js";
import { FakePool } from "./helpers.js";
import type { Pool } from "pg";

describe("getCollection SQL shape + params", () => {
  it("owner-scoped, no filters, value_desc: binds user_id, status whitelist, LIMIT/OFFSET; no interpolation", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { sort: "value_desc", limit: 60, offset: 0 });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/FROM cards c/i);
    expect(sql).toMatch(/JOIN photos p ON c\.photo_id\s*=\s*p\.id/i);
    expect(sql).toMatch(/JOIN batches b ON p\.batch_id\s*=\s*b\.id/i);
    expect(sql).toMatch(/JOIN card_refs r ON c\.card_ref_id\s*=\s*r\.id/i);
    expect(sql).toMatch(/LEFT JOIN prices pr ON pr\.card_ref_id\s*=\s*c\.card_ref_id AND pr\.finish\s*=\s*c\.finish/i);
    expect(sql).toMatch(/WHERE b\.user_id\s*=\s*\$1/i);
    expect(sql).toMatch(/c\.status IN \('auto','validated','corrected'\)/i);
    expect(sql).toMatch(/\(pr\.card_ref_id IS NOT NULL\) AS has_price_row/i);
    expect(sql).toMatch(/ORDER BY pr\.price_cents \* c\.quantity DESC NULLS LAST/i);
    expect(sql).toMatch(/LIMIT \$2 OFFSET \$3/i);
    expect(params).toEqual(["u1", 60, 0]);
    // no value ever concatenated into the SQL text
    expect(sql).not.toContain("u1");
  });

  it("all filters + name_asc: appends bound fragments in order, ORDER BY r.name ASC", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", {
      batchId: "b1", set: "base1", finish: "holofoil", source: "corrected", sort: "name_asc", limit: 60, offset: 60,
    });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/AND b\.id\s*=\s*\$2/i);
    expect(sql).toMatch(/AND r\.set_id\s*=\s*\$3/i);
    expect(sql).toMatch(/AND c\.finish\s*=\s*\$4/i);
    // source 'corrected' -> status IN ('validated','corrected')
    expect(sql).toMatch(/AND c\.status IN \('validated','corrected'\)/i);
    expect(sql).toMatch(/ORDER BY r\.name ASC/i);
    expect(sql).toMatch(/LIMIT \$5 OFFSET \$6/i);
    expect(params).toEqual(["u1", "b1", "base1", "holofoil", 60, 60]);
  });

  it("source 'auto' -> status='auto'; set_asc -> ORDER BY r.set_name, r.number", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { source: "auto", sort: "set_asc", limit: 60, offset: 0 });
    const { sql } = pool.calls[0];
    expect(sql).toMatch(/AND c\.status\s*=\s*'auto'/i);
    expect(sql).toMatch(/ORDER BY r\.set_name, r\.number/i);
  });

  it("unknown sort falls back to value_desc (whitelist, never interpolated)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { sort: "; DROP TABLE cards;--", limit: 60, offset: 0 });
    const { sql } = pool.calls[0];
    expect(sql).toMatch(/ORDER BY pr\.price_cents \* c\.quantity DESC NULLS LAST/i);
    expect(sql).not.toContain("DROP TABLE");
  });
});

describe("getCollectionStats", () => {
  it("owner-scoped aggregate: sums, priced fraction, oldest price; binds user_id", async () => {
    const pool = new FakePool();
    pool.enqueue({
      rows: [{ total_cards: 12, total_value_cents: 45600, priced_fraction: 0.75, oldest_price_at: "2026-07-15T00:00:00.000Z" }],
    });
    const stats = await getCollectionStats(pool as unknown as Pool, "u1", {});
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/COALESCE\(SUM\(c\.quantity\), 0\)/i);
    expect(sql).toMatch(/COALESCE\(SUM\(COALESCE\(pr\.price_cents, 0\) \* c\.quantity\), 0\)/i);
    expect(sql).toMatch(/MIN\(pr\.fetched_at\)/i);
    expect(sql).toMatch(/WHERE b\.user_id\s*=\s*\$1/i);
    expect(params).toEqual(["u1"]);
    expect(stats).toEqual({ total_cards: 12, total_value_cents: 45600, priced_fraction: 0.75, oldest_price_at: "2026-07-15T00:00:00.000Z" });
  });

  it("coerces numeric aggregates from pg strings (SUM/COUNT come back as text)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ total_cards: "12", total_value_cents: "45600", priced_fraction: "0.75", oldest_price_at: null }] });
    const stats = await getCollectionStats(pool as unknown as Pool, "u1", {});
    expect(stats.total_cards).toBe(12);
    expect(stats.total_value_cents).toBe(45600);
    expect(stats.priced_fraction).toBeCloseTo(0.75);
    expect(stats.oldest_price_at).toBeNull();
  });
});
```

- [ ] **Step 2: Run the query tests to verify they fail**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.query.test.ts
```

Expected: FAIL — `Failed to resolve import "../src/queries/collection.js"`.

- [ ] **Step 3: Implement `queries/collection.ts`**

Create `web/src/queries/collection.ts`:

```ts
import type { Pool } from "pg";

export interface CollectionRow {
  card_id: string;
  card_ref_id: string | null;
  crop_storage_key: string | null;
  name: string | null;
  set_name: string | null;
  number: string | null;
  finish: string | null;
  quantity: number;
  confidence: number;
  status: string;
  price_cents: number | null;
  price_source: string | null;
  price_fetched_at: string | null;
  batch_id: string;
  has_price_row: boolean;
}

export interface CollectionStats {
  total_cards: number;
  total_value_cents: number;
  priced_fraction: number;
  oldest_price_at: string | null;
}

export interface CollectionFilters {
  batchId?: string;
  set?: string;
  finish?: string;
  source?: "auto" | "corrected";
}

// ORDER BY whitelist. Keys are the only accepted sort values; the SQL fragment is fixed
// text (never interpolated from user input). Unknown -> value_desc.
const SORTS: Record<string, string> = {
  value_desc: "pr.price_cents * c.quantity DESC NULLS LAST",
  name_asc: "r.name ASC",
  set_asc: "r.set_name, r.number",
};

// Build the shared owner + filter WHERE clause with bound params. `start` is the next
// positional index ($1 is always user_id). Returns the clause text and the ordered params.
function whereClause(userId: string, opts: CollectionFilters): { sql: string; params: any[] } {
  const params: any[] = [userId];
  let sql = ` WHERE b.user_id = $1 AND c.status IN ('auto','validated','corrected')`;
  if (opts.batchId) {
    params.push(opts.batchId);
    sql += ` AND b.id = $${params.length}`;
  }
  if (opts.set) {
    params.push(opts.set);
    sql += ` AND r.set_id = $${params.length}`;
  }
  if (opts.finish) {
    params.push(opts.finish);
    sql += ` AND c.finish = $${params.length}`;
  }
  if (opts.source === "auto") {
    sql += ` AND c.status = 'auto'`;
  } else if (opts.source === "corrected") {
    sql += ` AND c.status IN ('validated','corrected')`;
  }
  return { sql, params };
}

export async function getCollection(
  pool: Pool,
  userId: string,
  opts: CollectionFilters & { sort: string; limit: number; offset: number },
): Promise<CollectionRow[]> {
  const { sql: where, params } = whereClause(userId, opts);
  const orderBy = SORTS[opts.sort] ?? SORTS.value_desc;
  params.push(opts.limit);
  const limitIdx = params.length;
  params.push(opts.offset);
  const offsetIdx = params.length;

  const sql =
    `SELECT c.id AS card_id, c.card_ref_id, c.crop_storage_key,
            r.name, r.set_name, r.number,
            c.finish, c.quantity, c.confidence, c.status,
            pr.price_cents, pr.source AS price_source, pr.fetched_at AS price_fetched_at,
            p.batch_id,
            (pr.card_ref_id IS NOT NULL) AS has_price_row
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where +
    ` ORDER BY ${orderBy} LIMIT $${limitIdx} OFFSET $${offsetIdx}`;

  const { rows } = await pool.query(sql, params);
  return rows as CollectionRow[];
}

export async function getCollectionStats(
  pool: Pool,
  userId: string,
  opts: CollectionFilters,
): Promise<CollectionStats> {
  const { sql: where, params } = whereClause(userId, opts);
  const sql =
    `SELECT COALESCE(SUM(c.quantity), 0) AS total_cards,
            COALESCE(SUM(COALESCE(pr.price_cents, 0) * c.quantity), 0) AS total_value_cents,
            CASE WHEN COUNT(*) = 0 THEN 0
                 ELSE COUNT(pr.price_cents)::float / COUNT(*) END AS priced_fraction,
            MIN(pr.fetched_at) AS oldest_price_at
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where;

  const { rows } = await pool.query(sql, params);
  const row = rows[0] ?? {};
  return {
    total_cards: Number(row.total_cards ?? 0),
    total_value_cents: Number(row.total_value_cents ?? 0),
    priced_fraction: Number(row.priced_fraction ?? 0),
    oldest_price_at: row.oldest_price_at ?? null,
  };
}
```

- [ ] **Step 4: Run the query tests to verify they pass**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.query.test.ts
```

Expected: PASS (6 passed).

- [ ] **Step 5: Write the failing route test**

Create `web/tests/collection.route.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { FakePool, makeDeps } from "./helpers.js";

const AUTHED_USER = { id: "u1", email: "a@b.com", tier: "free" };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

// Route issues getCollection() then getCollectionStats(): enqueue rows then the stats row.
function seed(pool: FakePool, rows: any[], stats: any) {
  pool.enqueue({ rows });
  pool.enqueue({ rows: [stats] });
}

const PRICED_ROW = {
  card_id: "c1", card_ref_id: "r1", crop_storage_key: "u1/b1/crops/c1.webp",
  name: "Charizard", set_name: "Base", number: "4", finish: "holofoil",
  quantity: 1, confidence: 0.98, status: "auto",
  price_cents: 1234, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z",
  batch_id: "b1", has_price_row: true,
};
const NO_DATA_ROW = { ...PRICED_ROW, card_id: "c2", name: "Pikachu", number: "58",
  price_cents: null, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z", has_price_row: true };
const PENDING_ROW = { ...PRICED_ROW, card_id: "c3", name: "Bulbasaur", number: "44",
  price_cents: null, price_source: null, price_fetched_at: null, has_price_row: false };
// Trailing-zero fixtures: (v/100)|round(2)|float in Nunjucks renders "$12.3" / "$12.0" for these —
// a naive template-arithmetic formatter would fail these two.
const WHOLE_DOLLAR_ROW = { ...PRICED_ROW, card_id: "c4", name: "Blastoise", number: "9",
  price_cents: 1200 };
const TRAILING_ZERO_ROW = { ...PRICED_ROW, card_id: "c5", name: "Venusaur", number: "15",
  price_cents: 1230 };

describe("GET /collection", () => {
  it("renders the grid, formats cents as $X.XX, and shows the stats bar", async () => {
    const pool = new FakePool();
    seed(pool, [PRICED_ROW, NO_DATA_ROW, PENDING_ROW], {
      total_cards: 3, total_value_cents: 1234, priced_fraction: 0.333, oldest_price_at: "2026-07-15T00:00:00.000Z",
    });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(200);
    expect(res.text).toContain("Charizard");
    expect(res.text).toContain("$12.34");        // 1234 cents formatted
    expect(res.text).toContain("no price data");  // price_cents NULL but has_price_row
    expect(res.text).toContain("pending price");  // no prices row at all
    expect(res.text).toContain("$12.34");         // stats total value
    expect(res.text).toContain("/img/crop/c1");   // grid thumb uses the user's crop
  });

  it("formats whole-dollar and trailing-zero cents correctly (never truncates the trailing zero)", async () => {
    const pool = new FakePool();
    seed(pool, [WHOLE_DOLLAR_ROW, TRAILING_ZERO_ROW], {
      total_cards: 2, total_value_cents: 2430, priced_fraction: 1, oldest_price_at: "2026-07-15T00:00:00.000Z",
    });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(200);
    expect(res.text).toContain("$12.00");   // 1200 cents — must NOT render "$12.0" or "$12"
    expect(res.text).toContain("$12.30");   // 1230 cents — must NOT render "$12.3"
    expect(res.text).toContain("$24.30");   // stats total value (2430 cents)
  });

  it("filters getCollection by user_id from the session (ownership, AC 7)", async () => {
    const pool = new FakePool();
    seed(pool, [], { total_cards: 0, total_value_cents: 0, priced_fraction: 0, oldest_price_at: null });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection");
    expect(pool.calls[0].params[0]).toBe("u1");
    expect(pool.calls[1].params[0]).toBe("u1");
  });

  it("passes sort/filter query params through to the query", async () => {
    const pool = new FakePool();
    seed(pool, [], { total_cards: 0, total_value_cents: 0, priced_fraction: 0, oldest_price_at: null });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection?sort=name_asc&set=base1&finish=holofoil&source=corrected&batch=b1&page=2");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/ORDER BY r\.name ASC/i);
    expect(params).toContain("base1");
    expect(params).toContain("holofoil");
    expect(params).toContain("b1");
    // page 2 with page_size 60 -> offset 60
    expect(params).toContain(60);
  });

  it("302 redirects anonymous users to /", async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
```

- [ ] **Step 6: Run the route test to verify it fails**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.route.test.ts
```

Expected: FAIL — `GET /collection` is not mounted, so it 302-redirects (anon case passes) but the authed cases fall through to `notFound()` (404) instead of 200.

- [ ] **Step 7: Add the `explorer` config type**

In `web/src/config.ts`, add to the `Config` interface (after `refproxy`, added in Task 5):

```ts
  refproxy: { allowed_image_host: string; cache_prefix: string; max_bytes: number };
  explorer: { page_size: number; default_sort: string };
}
```

And in `web/tests/helpers.ts`, add to `testCfg` (after the `refproxy` block from Task 5):

```ts
  refproxy: { allowed_image_host: "images.pokemontcg.io", cache_prefix: "refs", max_bytes: 5242880 },
  explorer: { page_size: 60, default_sort: "value_desc" },
} as unknown as Config;
```

- [ ] **Step 8: Create the shared `formatCents` helper (`web/src/lib/money.ts`)**

Money formatting lives in exactly one place, imported by both this task's route and Task 7's CSV
export — never as inline Nunjucks arithmetic (`(v/100)|round(2)|float` truncates trailing zeros,
e.g. renders `$12.3` for 1230 cents and `$12.0` for 1200 cents) and never duplicated per-caller.

Create `web/tests/money.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { formatCents } from "../src/lib/money.js";

describe("formatCents", () => {
  it("formats cents as $X.XX", () => {
    expect(formatCents(1234)).toBe("$12.34");
  });
  it("keeps the trailing zero for whole dollars", () => {
    expect(formatCents(1200)).toBe("$12.00");
  });
  it("keeps the trailing zero for a single-cent-digit remainder", () => {
    expect(formatCents(1230)).toBe("$12.30");
  });
  it("formats zero cents as $0.00", () => {
    expect(formatCents(0)).toBe("$0.00");
  });
});
```

Run it to verify it fails (`Failed to resolve import "../src/lib/money.js"`), then create
`web/src/lib/money.ts`:

```ts
// Single source of truth for cents -> dollar-string formatting. Non-null only: callers decide
// how to render a null price (view: "no price data" / "pending price"; CSV: "").
export function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
```

Run `web/tests/money.test.ts` again to verify it passes (4 passed).

- [ ] **Step 9: Implement `routes/collection.ts` (GET /collection only — CSV added in Task 7)**

Create `web/src/routes/collection.ts`:

```ts
import { Router } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { AuthedRequest } from "../middleware/session.js";
import { requireUser } from "../middleware/session.js";
import { getCollection, getCollectionStats, type CollectionFilters, type CollectionRow } from "../queries/collection.js";
import { formatCents } from "../lib/money.js";

const SORTS = new Set(["value_desc", "name_asc", "set_asc"]);

// Read one string query param, treating "" as absent.
function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

// Parse the shared filter params (batch/set/finish/source) into a CollectionFilters.
function parseFilters(q: any): CollectionFilters {
  const source = q.source === "auto" || q.source === "corrected" ? q.source : undefined;
  return { batchId: str(q.batch), set: str(q.set), finish: str(q.finish), source };
}

// View-only display string for a row's price: "pending price" (no prices row yet),
// "no price data" (row exists, price_cents NULL), or the formatted dollar amount.
// The ONLY place explorer rows get their money string — never format in the template.
function priceDisplay(row: CollectionRow): string {
  if (!row.has_price_row) return "pending price";
  if (row.price_cents == null) return "no price data";
  return formatCents(row.price_cents);
}

export function collectionRouter(pool: Pool, cfg: Config): Router {
  const r = Router();

  r.get("/collection", requireUser(), async (req: AuthedRequest, res) => {
    const q = req.query as any;
    const filters = parseFilters(q);
    const sort = SORTS.has(q.sort) ? (q.sort as string) : cfg.explorer.default_sort;
    const pageSize = cfg.explorer.page_size;
    const page = Math.max(1, Number.parseInt(String(q.page ?? "1"), 10) || 1);
    const offset = (page - 1) * pageSize;

    const rows = await getCollection(pool, req.user!.id, {
      ...filters,
      sort,
      limit: pageSize,
      offset,
    });
    const stats = await getCollectionStats(pool, req.user!.id, filters);

    res.render("collection.njk", {
      rows: rows.map((row) => ({ ...row, price_display: priceDisplay(row) })),
      stats: { ...stats, total_value_display: formatCents(stats.total_value_cents) },
      filters: { ...filters, sort },
      page,
      pageSize,
      hasNext: rows.length === pageSize,
    });
  });

  return r;
}
```

- [ ] **Step 10: Mount `collectionRouter` in `app.ts`**

Modify `web/src/app.ts`. Add the import next to the other route imports:

```ts
import { collectionRouter } from "./routes/collection.js";
```

And mount it at the app level (no prefix — the router owns its full `/collection` path and applies its own `requireUser()` per route, mirroring `validateRouter`). Add after `app.use(validateRouter(pool, cfg));`:

```ts
  app.use(validateRouter(pool, cfg));
  app.use(collectionRouter(pool, cfg));
  app.use(resultsRouter(pool));
```

- [ ] **Step 11: Create the `collection.njk` view**

Create `web/views/collection.njk`. Pure query-param links for sort/filter — no inline JS (CSP-safe). Money is
pre-formatted by the route (`row.price_display`, `stats.total_value_display`, via the shared `formatCents`
in `web/src/lib/money.ts`) — the template does no cents arithmetic, only freshness formatting for
`oldest_price_at`.

```njk
{% extends "layout.njk" %}
{% block title %}Collection — NotBulk{% endblock %}

{% macro sortlink(key, label) %}
  <a href="?sort={{ key }}{% if filters.batchId %}&batch={{ filters.batchId }}{% endif %}{% if filters.set %}&set={{ filters.set }}{% endif %}{% if filters.finish %}&finish={{ filters.finish }}{% endif %}{% if filters.source %}&source={{ filters.source }}{% endif %}"
     class="sortlink{% if filters.sort == key %} active{% endif %}">{{ label }}</a>
{% endmacro %}

{% block content %}
<section class="collection">
  <h1>Your collection</h1>

  <div class="stats-bar">
    <span class="stat"><strong>{{ stats.total_cards }}</strong> cards</span>
    <span class="stat"><strong>{{ stats.total_value_display }}</strong> total value</span>
    <span class="stat"><strong>{{ (stats.priced_fraction * 100) | round(0) | int }}%</strong> priced</span>
    <span class="stat">
      {% if stats.oldest_price_at %}
        prices as of {{ stats.oldest_price_at | replace("T", " ") | truncate(16, true, "") }}
      {% else %}
        no prices yet
      {% endif %}
    </span>
  </div>

  <nav class="sort-controls">
    Sort:
    {{ sortlink("value_desc", "Value") }}
    {{ sortlink("name_asc", "Name") }}
    {{ sortlink("set_asc", "Set") }}
  </nav>

  <form class="filter-controls" method="get" action="/collection">
    <input type="hidden" name="sort" value="{{ filters.sort }}" />
    <label>Set <input type="text" name="set" value="{{ filters.set or '' }}" placeholder="set id" /></label>
    <label>Finish
      <select name="finish">
        <option value=""{% if not filters.finish %} selected{% endif %}>any</option>
        <option value="normal"{% if filters.finish == 'normal' %} selected{% endif %}>normal</option>
        <option value="holofoil"{% if filters.finish == 'holofoil' %} selected{% endif %}>holofoil</option>
        <option value="reverseHolofoil"{% if filters.finish == 'reverseHolofoil' %} selected{% endif %}>reverse holo</option>
      </select>
    </label>
    <label>Source
      <select name="source">
        <option value=""{% if not filters.source %} selected{% endif %}>all</option>
        <option value="auto"{% if filters.source == 'auto' %} selected{% endif %}>auto-accepted</option>
        <option value="corrected"{% if filters.source == 'corrected' %} selected{% endif %}>reviewed</option>
      </select>
    </label>
    <button type="submit">Apply</button>
    <a class="clear" href="/collection">Clear</a>
  </form>

  {% if rows.length == 0 %}
    <p class="empty">No cards match. Scan a batch or clear your filters.</p>
  {% else %}
    <ul class="card-grid">
      {% for row in rows %}
        <li class="card-cell">
          <img src="/img/crop/{{ row.card_id }}" alt="{{ row.name or 'card' }}" loading="lazy" />
          <div class="card-name">{{ row.name or 'Unknown' }}</div>
          <div class="card-set">{{ row.set_name }} &middot; {{ row.number }}</div>
          <div class="card-finish">{{ row.finish or '—' }}</div>
          <div class="card-price">
            {% if not row.has_price_row %}
              <span class="pending">{{ row.price_display }}</span>
            {% elif row.price_cents == null %}
              <span class="nodata">{{ row.price_display }}</span>
            {% else %}
              <span class="price">{{ row.price_display }}</span>
            {% endif %}
          </div>
          {% if row.quantity > 1 %}<div class="card-qty">×{{ row.quantity }}</div>{% endif %}
        </li>
      {% endfor %}
    </ul>

    <nav class="pager">
      {% if page > 1 %}
        <a href="?page={{ page - 1 }}&sort={{ filters.sort }}{% if filters.batchId %}&batch={{ filters.batchId }}{% endif %}{% if filters.set %}&set={{ filters.set }}{% endif %}{% if filters.finish %}&finish={{ filters.finish }}{% endif %}{% if filters.source %}&source={{ filters.source }}{% endif %}">&larr; Prev</a>
      {% endif %}
      <span class="page-n">Page {{ page }}</span>
      {% if hasNext %}
        <a href="?page={{ page + 1 }}&sort={{ filters.sort }}{% if filters.batchId %}&batch={{ filters.batchId }}{% endif %}{% if filters.set %}&set={{ filters.set }}{% endif %}{% if filters.finish %}&finish={{ filters.finish }}{% endif %}{% if filters.source %}&source={{ filters.source }}{% endif %}">Next &rarr;</a>
      {% endif %}
    </nav>

    <p class="export"><a href="/collection/export.csv">Download CSV</a></p>
  {% endif %}
</section>
<script src="/js/collection.js" defer></script>
{% endblock %}
```

- [ ] **Step 12: Create `public/js/collection.js` (CSP-safe, no inline)**

The explorer needs no JS beyond the GET form + query-param links (all server-rendered). Ship a
static no-op file so the `<script src="/js/collection.js">` tag resolves (matching the `validate.js`
convention) and there is a home for future progressive enhancement.

Create `web/public/js/collection.js`:

```js
// Collection explorer is fully server-rendered: sort/filter are query-param links and a
// GET form, so no JavaScript is required for core behavior (CSP-safe, no inline handlers).
// This file exists as the mount point for future progressive enhancement (e.g. live filter
// preview) and to satisfy the <script src="/js/collection.js"> reference in collection.njk.
"use strict";
```

- [ ] **Step 13: Run the route test to verify it passes**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.route.test.ts
```

Expected: PASS (5 passed — includes the whole-dollar/trailing-zero case).

- [ ] **Step 14: Run the full web suite**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run
```

Expected: PASS.

- [ ] **Step 15: Commit**

```bash
git add web/src/lib/money.ts web/src/queries/collection.ts web/src/routes/collection.ts web/src/app.ts web/src/config.ts \
        web/views/collection.njk web/public/js/collection.js \
        web/tests/money.test.ts web/tests/collection.query.test.ts web/tests/collection.route.test.ts web/tests/helpers.ts
git commit -m "feat(web): collection explorer with pricing, sort, and filters"
```

---

### Task 7: CSV export (`GET /collection/export.csv`)

**Files:**
- Modify: `web/src/queries/collection.ts` (add `getCollectionForExport` — unpaginated, owner-scoped)
- Modify: `web/src/routes/collection.ts` (add `GET /collection/export.csv` + `csvCell`; reuses the
  `formatCents` helper from `web/src/lib/money.ts`, created in Task 6 — no second implementation)
- Test: `web/tests/collection.export.test.ts` (route — exact CSV bytes, quoting, empty null-price cell, ownership)

**Interfaces:**
- Consumes: `getCollectionForExport(pool, userId, opts)` (new), `CollectionRow`, `CollectionFilters`.
- Produces:

```ts
export async function getCollectionForExport(
  pool: Pool, userId: string, opts: CollectionFilters,
): Promise<CollectionRow[]>;   // same JOINs/status filter as getCollection, ORDER BY r.set_name, r.number; NO LIMIT/OFFSET
```

- Route `GET /collection/export.csv` (`requireUser`, owner-scoped) → streamed `text/csv` with columns
  (VERBATIM, §6.6): `name, set, number, finish, quantity, price, price_source, price_date, confidence, batch, image_filename`.

**Column mapping:**
- `price` = `csvPrice(price_cents)` — delegates to the shared `formatCents` (from `web/src/lib/money.ts`,
  Task 6) for the non-null case → `$X.XX`, or `''` (empty, **never** `$0.00`) when `price_cents` is `null`.
- `image_filename` = `${card_id}.webp` (the crop basename).
- `price_date` = `price_fetched_at` (raw ISO string, or `''` when null).
- `set` = `set_name`; `batch` = `batch_id`.
- Only `auto`/`validated`/`corrected` cards (enforced by the shared `whereClause`).

**Filename:** static `notbulk-collection.csv` — the plan context has no injectable clock, and a
date-stamped name would be nondeterministic in tests. `Content-Disposition: attachment; filename="notbulk-collection.csv"`.

**Quoting:** RFC 4180 via `csvCell(value)` — wrap in double-quotes and double any internal double-quote
when the value contains a comma, double-quote, CR, or LF; otherwise emit as-is.

- [ ] **Step 1: Write the failing export test**

Create `web/tests/collection.export.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { FakePool, makeDeps } from "./helpers.js";

const AUTHED_USER = { id: "u1", email: "a@b.com", tier: "free" };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

const HEADER = "name,set,number,finish,quantity,price,price_source,price_date,confidence,batch,image_filename";

const PRICED = {
  card_id: "c1", card_ref_id: "r1", crop_storage_key: "u1/b1/crops/c1.webp",
  name: "Charizard", set_name: "Base", number: "4", finish: "holofoil",
  quantity: 2, confidence: 0.98, status: "auto",
  price_cents: 1234, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z",
  batch_id: "b1", has_price_row: true,
};
const NULL_PRICE = { ...PRICED, card_id: "c2", name: "Pikachu", number: "58",
  price_cents: null, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z" };
const COMMA_NAME = { ...PRICED, card_id: "c3", name: "Mr. Mime, Prime", number: "63",
  price_cents: 500 };

describe("GET /collection/export.csv", () => {
  it("streams the exact header + rows, formats price, empty cell for null, quotes commas", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [PRICED, NULL_PRICE, COMMA_NAME] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection/export.csv");

    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/csv/);
    expect(res.headers["content-disposition"]).toBe('attachment; filename="notbulk-collection.csv"');

    const lines = res.text.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");
    expect(lines[0]).toBe(HEADER);
    // priced row: $12.34, image basename c1.webp
    expect(lines[1]).toBe("Charizard,Base,4,holofoil,2,$12.34,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c1.webp");
    // null price -> empty cell (NOT $0.00), between quantity and price_source
    expect(lines[2]).toBe("Pikachu,Base,58,holofoil,2,,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c2.webp");
    // comma in name -> whole field quoted (RFC 4180)
    expect(lines[3]).toBe('"Mr. Mime, Prime",Base,63,holofoil,2,$5.00,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c3.webp');
  });

  it("null price cell is empty string, never $0.00", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [NULL_PRICE] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection/export.csv");
    expect(res.text).not.toContain("$0.00");
    const cells = res.text.replace(/\r\n/g, "\n").split("\n")[1].split(",");
    expect(cells[5]).toBe(""); // price column empty
  });

  it("filters the export query by user_id (ownership, AC 7)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection/export.csv");
    expect(pool.calls[0].params[0]).toBe("u1");
    expect(pool.calls[0].sql).not.toMatch(/LIMIT/i); // export is the full collection
  });

  it("302 redirects anonymous users to /", async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await request(app).get("/collection/export.csv");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
```

- [ ] **Step 2: Run the export test to verify it fails**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.export.test.ts
```

Expected: FAIL — `GET /collection/export.csv` not mounted; authed cases 404, anon case passes.

- [ ] **Step 3: Add `getCollectionForExport` to `queries/collection.ts`**

In `web/src/queries/collection.ts`, add at the end of the file (reuses the module-private `whereClause` from Task 6):

```ts
// Full unpaginated collection for CSV export: same owner + status filter as getCollection,
// deterministic ORDER BY, NO LIMIT/OFFSET (the export is the entire collection).
export async function getCollectionForExport(
  pool: Pool,
  userId: string,
  opts: CollectionFilters,
): Promise<CollectionRow[]> {
  const { sql: where, params } = whereClause(userId, opts);
  const sql =
    `SELECT c.id AS card_id, c.card_ref_id, c.crop_storage_key,
            r.name, r.set_name, r.number,
            c.finish, c.quantity, c.confidence, c.status,
            pr.price_cents, pr.source AS price_source, pr.fetched_at AS price_fetched_at,
            p.batch_id,
            (pr.card_ref_id IS NOT NULL) AS has_price_row
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where +
    ` ORDER BY r.set_name, r.number`;
  const { rows } = await pool.query(sql, params);
  return rows as CollectionRow[];
}
```

- [ ] **Step 4: Add the export route + CSV helpers to `routes/collection.ts`**

In `web/src/routes/collection.ts`, update the import to pull in `getCollectionForExport` (the
`formatCents` import from Task 6 already covers this task — same helper, not redefined):

```ts
import { getCollection, getCollectionStats, getCollectionForExport, type CollectionFilters, type CollectionRow } from "../queries/collection.js";
```

Add this module-level helper (below the `priceDisplay` function, above `collectionRouter`):

```ts
// CSV boundary: null price_cents -> "" (never "$0.00"). Delegates to the shared formatCents
// for the non-null case — same helper the explorer view uses via priceDisplay().
function csvPrice(cents: number | null): string {
  return cents == null ? "" : formatCents(cents);
}

// RFC 4180: quote a field containing comma/quote/CR/LF; double any internal quote.
function csvCell(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}
```

Add the route inside `collectionRouter`, before `return r;`:

```ts
  r.get("/collection/export.csv", requireUser(), async (req: AuthedRequest, res) => {
    const rows = await getCollectionForExport(pool, req.user!.id, parseFilters(req.query as any));

    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader("Content-Disposition", 'attachment; filename="notbulk-collection.csv"');

    // §6.6 column order (verbatim).
    res.write(
      "name,set,number,finish,quantity,price,price_source,price_date,confidence,batch,image_filename\r\n",
    );
    for (const row of rows) {
      const cells = [
        row.name ?? "",
        row.set_name ?? "",
        row.number ?? "",
        row.finish ?? "",
        String(row.quantity),
        csvPrice(row.price_cents),
        row.price_source ?? "",
        row.price_fetched_at ?? "",
        String(row.confidence),
        row.batch_id,
        `${row.card_id}.webp`,
      ];
      res.write(cells.map((c) => csvCell(String(c))).join(",") + "\r\n");
    }
    res.end();
  });
```

- [ ] **Step 5: Run the export test to verify it passes**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/collection.export.test.ts
```

Expected: PASS (4 passed).

- [ ] **Step 6: Run the full web suite**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/queries/collection.ts web/src/routes/collection.ts web/tests/collection.export.test.ts
git commit -m "feat(web): streamed CSV collection export"
```
<!-- TASK SECTIONS 8-10 — assembled into 2026-07-16-m3-pricing-explorer.md below the header. -->
<!-- These conform to the plan's Interface Contract (authoritative). Do not restate the header here. -->

### Task 8: `handlers/finish.py` — finish-spread narrowing (INVARIANT-SENSITIVE)

This task implements the finish-spread narrowing that `handle_price` (Task 3) calls after
every upsert. It is **zero-wrong-auto-accept-adjacent**: it may only ever CLEAR a
finish-confirmation flag and downgrade a validation-due-to-finish card toward `auto`. It must
be *provably conservative* — the test suite is the point of this task.

**Read the Interface Contract's AUTHORITATIVE RULE (plan §"Python signatures", `maybe_narrow_finish_flag`)
and Global Constraint #2 before writing a line.** The rule, restated as executable logic:

- Candidate rows are ONLY those `WHERE card_ref_id=$1 AND status='validation' AND
  finish_needs_confirmation=true AND accepted_stage IN ('h','multi','llm')`. A card in
  `validation` for identity reasons (`accepted_stage='validation'`), or already
  `validated`/`corrected`/`skipped`/`merged`/`not_card`/`unreadable`, is NEVER a candidate.
- For each candidate, read `card_refs.finishes` and the cached `prices` rows for exactly those
  finishes. Compute the spread ONLY IF **every** finish in the list has a non-NULL
  `price_cents` present in the cache. A NULL known-miss OR an absent row ⇒ cannot compute ⇒
  leave the card untouched.
- `spread_pct = (max - min) / min * 100`, guarded on `min > 0`. If `min == 0` ⇒ cannot narrow ⇒
  leave untouched.
- If `spread_pct <= cfg["pricing"]["finish_spread_flag_pct"]` (15): the finish barely affects
  value, so set `finish` = the FIRST key of `FINISH_KEYS` order that is both in the card's
  finishes AND priced, set `status='auto'`, `finish_needs_confirmation=false`. Otherwise leave
  untouched.
- The narrowing UPDATE re-checks its own preconditions in the WHERE clause
  (`WHERE id=%s AND status='validation' AND finish_needs_confirmation=true`) so a concurrent
  human validation cannot be clobbered — the narrow is a no-op if the row moved on.
- NEVER change `card_ref_id`; NEVER touch `confidence`, `accepted_stage`, or `candidates`.

**Files:**
- Create: `worker/notbulk/handlers/finish.py`
- Test: `worker/tests/test_handler_finish.py`

**Interfaces:**
- Consumes:
  - `FINISH_KEYS = ("normal", "holofoil", "reverseHolofoil")` from `notbulk.pricing.sources`
    (Task 2) — tuple defining the canonical narrowing precedence.
  - `cfg["pricing"]["finish_spread_flag_pct"]` (int `15`) from Task 1's `config.yaml` addition.
  - A psycopg-style `pool` whose `pool.connection()` yields a context-manager connection with
    `.cursor()` (also a context manager) exposing `.execute(sql, params)` / `.fetchone()` /
    `.fetchall()`, and `.commit()` on the connection — matches `worker/tests/fakes.py:FakePool`
    and `handlers/identify.py` usage.
- Produces:
  - `def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None` — called by
    `handle_price` (Task 3) after each `upsert_price`. Idempotent; a no-op when no candidate row
    qualifies.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_handler_finish.py`. These tests use no DB — a `ScriptedPool` whose
responder matches on SQL text (same pattern as `test_handler_identify.py`) returns the candidate
rows, the `card_refs.finishes`, and the cached `prices` rows; then the test asserts on the
recorded UPDATE (its SQL text and params) — or asserts NO `UPDATE cards` ran at all.

```python
"""maybe_narrow_finish_flag: the finish-spread narrowing invariant guard (Task 8).

Zero wrong auto-accepts is the hard invariant. These tests cover the guard hard:
only 'validation' + finish_needs_confirmation + accepted_stage in (h,multi,llm) cards
are candidates; the flag clears ONLY when every finish is priced and the spread is
<=15%; and the narrowing UPDATE re-checks its own preconditions in the WHERE clause.

No DB: a SQL-text-matching scripted pool returns candidate rows, the card_refs
finishes, and the cached prices; assertions read the recorded UPDATE (or its absence).
"""
from __future__ import annotations

from notbulk.handlers import finish as fh


class ScriptedCursor:
    def __init__(self, responder):
        self._responder = responder
        self._current = []
        self.executed = []  # list[(sql, params)] for assertions
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responder(sql, params)
        self.rowcount = len(self._current)
        return self

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedPool:
    def __init__(self, responder):
        self.cursor = ScriptedCursor(responder)
        self._conn = ScriptedConn(self.cursor)

    def connection(self):
        return self._conn


CFG = {"pricing": {"finish_spread_flag_pct": 15}}


def _responder(*, candidates, finishes, prices):
    """Build a SQL-text responder.

    candidates: rows for the candidate SELECT — each (card_id, ) tuple.
    finishes:   list[str] returned for the card_refs.finishes SELECT.
    prices:     list[(finish, price_cents)] returned for the prices SELECT
                (omit a finish entirely to model an absent row; use None cents
                to model a NULL cached known-miss).
    """
    def responder(sql, params):
        low = " ".join(sql.lower().split())
        if low.startswith("select") and "from cards" in low and "finish_needs_confirmation" in low:
            return list(candidates)
        if "select finishes from card_refs" in low:
            return [(finishes,)]
        if low.startswith("select") and "from prices" in low:
            return list(prices)
        # UPDATE cards -> pretend one row matched (fired); the guard WHERE is asserted by text.
        return [("card-1",)]
    return responder


def _update(pool):
    for sql, params in pool.cursor.executed:
        if sql.lower().startswith("update cards"):
            return sql, params
    return None, None


def test_two_finishes_small_spread_narrows_to_auto():
    # normal=1000c, holofoil=1100c -> spread 10% <= 15% -> narrow.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1100)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, params = _update(pool)
    assert sql is not None
    assert "finish_needs_confirmation=false" in sql.lower()
    assert "status='auto'" in sql.lower() or "status = 'auto'" in sql.lower()
    # finish set to FIRST FINISH_KEYS-order present+priced key = 'normal'.
    assert "normal" in params
    assert "card-1" in params


def test_large_spread_untouched():
    # normal=1000c, holofoil=1250c -> spread 25% > 15% -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1250)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None  # no narrowing UPDATE ran


def test_one_finish_null_price_untouched():
    # holofoil is a cached NULL known-miss -> cannot compute -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", None)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_one_finish_absent_from_cache_untouched():
    # holofoil has no prices row at all -> cannot compute -> untouched.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_min_price_zero_untouched():
    # normal=0c -> min==0 -> spread undefined -> untouched (never divide by zero).
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 0), ("holofoil", 500)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None


def test_no_candidates_is_a_noop():
    # A card already validated by a human is not returned by the candidate SELECT
    # (status<>'validation'); the candidate query returns nothing -> no reads, no UPDATE.
    pool = ScriptedPool(_responder(
        candidates=[],           # SELECT ... status='validation' ... -> no rows
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1010)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    assert sql is None
    # The candidate SELECT ran; nothing else touched cards.
    assert any(s.lower().startswith("select") and "from cards" in s.lower()
               for s, _ in pool.cursor.executed)


def test_candidate_select_scopes_status_flag_and_stage():
    # The candidate SELECT itself must encode the guard: status='validation',
    # finish_needs_confirmation, accepted_stage IN ('h','multi','llm'). This proves
    # ID-uncertain (accepted_stage='validation'), already-validated, and
    # flag=false cards are excluded at the source query.
    pool = ScriptedPool(_responder(candidates=[], finishes=[], prices=[]))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sel = next(s for s, _ in pool.cursor.executed
               if s.lower().startswith("select") and "from cards" in s.lower())
    low = " ".join(sel.lower().split())
    assert "status='validation'" in low or "status = 'validation'" in low
    assert "finish_needs_confirmation" in low
    assert "accepted_stage in ('h','multi','llm')" in low or \
           "accepted_stage in ('h', 'multi', 'llm')" in low


def test_narrowing_update_where_rechecks_preconditions():
    # The guarded UPDATE re-checks status+flag in its WHERE so a concurrent human
    # validation can't be clobbered. Assert the SQL text.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["normal", "holofoil"],
        prices=[("normal", 1000), ("holofoil", 1050)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    sql, _ = _update(pool)
    low = " ".join(sql.lower().split())
    assert "where id=" in low or "where id =" in low
    assert "status='validation'" in low or "status = 'validation'" in low
    assert "finish_needs_confirmation=true" in low or "finish_needs_confirmation = true" in low


def test_first_finish_keys_order_wins_when_narrowing():
    # Card finishes listed holofoil-first, but FINISH_KEYS precedence puts 'normal'
    # first; the chosen finish must follow FINISH_KEYS order, not the row's order.
    pool = ScriptedPool(_responder(
        candidates=[("card-1",)],
        finishes=["holofoil", "normal"],
        prices=[("holofoil", 1100), ("normal", 1000)],
    ))
    fh.maybe_narrow_finish_flag(pool, "sv4-1", CFG)
    _sql, params = _update(pool)
    assert "normal" in params
    assert "holofoil" not in params
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd worker && uv run pytest tests/test_handler_finish.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'notbulk.handlers.finish'`
(the module does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `worker/notbulk/handlers/finish.py` with the complete narrowing logic. Note the
candidate SELECT and the guarded UPDATE both encode the invariant; the spread math is pure and
tiny.

```python
"""Finish-spread narrowing (design §4.4 / M2 amendment #3). INVARIANT-SENSITIVE.

Called by handle_price (handlers/price.py) after every price upsert. It may ONLY
ever clear a finish-confirmation flag and downgrade a validation-due-to-finish
card toward 'auto'. Zero wrong auto-accepts is the hard invariant, so this code
is provably conservative:

- Candidates are ONLY cards WHERE card_ref_id=? AND status='validation' AND
  finish_needs_confirmation=true AND accepted_stage IN ('h','multi','llm').
  A card in validation for identity reasons (accepted_stage='validation'), or
  already validated/corrected/skipped/merged/not_card/unreadable, is NEVER touched.
- The flag clears ONLY when EVERY finish of the card's card_refs.finishes has a
  non-NULL cached price AND the spread across those prices is <= the configured
  pct (15). A NULL known-miss, an absent price row, min==0, or spread>pct all
  leave the card untouched (it stays in validation for a human).
- On narrow: finish := first FINISH_KEYS-order key that is both in the card's
  finishes and priced; status:='auto'; finish_needs_confirmation:=false.
  card_ref_id, confidence, accepted_stage, and candidates are never touched.
- The UPDATE re-checks status='validation' AND finish_needs_confirmation=true in
  its WHERE so a concurrent human validation cannot be clobbered (the narrow is a
  no-op if the row already moved on).
"""
from __future__ import annotations

from ..pricing.sources import FINISH_KEYS

# Candidate cards: encodes the full guard at the source query. Only these rows can
# ever be narrowed.
_SELECT_CANDIDATES_SQL = (
    "SELECT id FROM cards "
    "WHERE card_ref_id=%s AND status='validation' AND finish_needs_confirmation=true "
    "AND accepted_stage IN ('h','multi','llm')"
)

_SELECT_FINISHES_SQL = "SELECT finishes FROM card_refs WHERE id = %s"

# Cached prices for exactly the card's finishes. Absent finish -> no row returned
# (=> cannot compute); NULL price_cents -> known-miss (=> cannot compute).
_SELECT_PRICES_SQL = (
    "SELECT finish, price_cents FROM prices "
    "WHERE card_ref_id=%s AND finish = ANY(%s)"
)

# Guarded narrow: the WHERE re-checks the preconditions so a concurrent validation
# cannot be clobbered (atomic re-check). Only clears the flag; never touches
# card_ref_id / confidence / accepted_stage / candidates.
_NARROW_SQL = (
    "UPDATE cards SET finish=%s, finish_needs_confirmation=false, status='auto', "
    "updated_at=now() "
    "WHERE id=%s AND status='validation' AND finish_needs_confirmation=true"
)


def maybe_narrow_finish_flag(pool, card_ref_id: str, cfg: dict) -> None:
    threshold = cfg["pricing"]["finish_spread_flag_pct"]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_CANDIDATES_SQL, (card_ref_id,))
            candidate_ids = [r[0] for r in cur.fetchall()]
        conn.commit()

    if not candidate_ids:
        return

    # card_refs is global reference data: finishes + prices are the same for every
    # candidate card of this card_ref_id, so read them once.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_FINISHES_SQL, (card_ref_id,))
            frow = cur.fetchone()
        conn.commit()
    finishes = list(frow[0]) if frow and frow[0] else []
    if not finishes:
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_PRICES_SQL, (card_ref_id, finishes))
            price_rows = cur.fetchall()
        conn.commit()

    # Map finish -> price_cents for the cached rows we got back.
    priced: dict[str, int | None] = {f: c for (f, c) in price_rows}

    # Every finish must be present AND non-NULL, else we cannot compute -> untouched.
    if any(f not in priced or priced[f] is None for f in finishes):
        return

    values = [priced[f] for f in finishes]
    lo, hi = min(values), max(values)
    if lo <= 0:
        return  # min==0 (or negative, defensively) -> spread undefined -> untouched
    spread_pct = (hi - lo) / lo * 100.0
    if spread_pct > threshold:
        return  # finish materially affects value -> stays in validation

    # Narrow: pick the FIRST FINISH_KEYS-order key that is both a card finish and priced.
    chosen = next((k for k in FINISH_KEYS if k in finishes and priced.get(k) is not None), None)
    if chosen is None:
        return  # a card finish outside FINISH_KEYS with none of the known keys -> untouched

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for cid in candidate_ids:
                cur.execute(_NARROW_SQL, (chosen, cid))
        conn.commit()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd worker && uv run pytest tests/test_handler_finish.py -v
```
Expected: PASS — all 9 tests green (`test_two_finishes_small_spread_narrows_to_auto`,
`test_large_spread_untouched`, `test_one_finish_null_price_untouched`,
`test_one_finish_absent_from_cache_untouched`, `test_min_price_zero_untouched`,
`test_no_candidates_is_a_noop`, `test_candidate_select_scopes_status_flag_and_stage`,
`test_narrowing_update_where_rechecks_preconditions`,
`test_first_finish_keys_order_wins_when_narrowing`).

- [ ] **Step 5: Run the full worker suite + eval to confirm no regression**

Run:
```bash
cd worker && uv run pytest tests ../eval/tests
```
Expected: PASS — the new finish tests plus all prior worker/eval tests green (eval regression
harness unchanged; this task adds no pipeline model behavior).

- [ ] **Step 6: Commit**

```bash
git add worker/notbulk/handlers/finish.py worker/tests/test_handler_finish.py
git commit -m "fix(worker): narrow finish-confirmation flag by 15% price spread"
```

---

### Task 9: Validation UI reference thumbnail

The M2 validation UI showed candidate cards as TEXT ONLY because external images were
CSP-blocked (Assembly Resolution 9). Task 5 built the `/img/ref/:cardRefId` proxy that serves
reference art from MinIO under the existing CSP (`img-src 'self' http://127.0.0.1:9000`). This
task adds a reference thumbnail per candidate via `<img src="/img/ref/{{ opt.id }}">`.

Keep it simple: no inline JS (CSP forbids inline `onerror` handlers), so a 404 from the proxy
just renders as the browser's broken-image glyph — acceptable per the plan. No new route logic;
Task 5 owns `/img/ref`.

**Files:**
- Modify: `web/views/partials/validate-card.njk` (add the `<img>` per candidate; drop the
  "text only" comment)
- Modify: `web/tests/validate.route.test.ts` (flip the M2 `not.toContain(... img ...)` /
  text-only assertion to assert the proxy `<img>` is present)

**Interfaces:**
- Consumes:
  - `GET /img/ref/:cardRefId` (Task 5) — 302 to a signed MinIO URL, or 404 when the reference
    image cannot be cached. No auth params in the URL; `requireUser` gates the route.
  - `options` from `web/src/routes/validate.ts:46` — an array of `{ id, name, set_name, number,
    finishes }` objects (already passed to `validate.njk`). Each candidate's `opt.id` is the
    `card_ref_id` the proxy takes.
- Produces:
  - No new exported symbols. The rendered `validate.njk` now contains one
    `<img src="/img/ref/{opt.id}">` per candidate, all same-origin (proxy), none pointing at
    `images.pokemontcg.io`.

- [ ] **Step 1: Update the failing test first (flip the M2 text-only assertion)**

In `web/tests/validate.route.test.ts`, the test
`renders the earliest validation card with top candidate + alternates (text only)` currently
asserts the reference image is absent. Replace that test body's assertions so it requires the
proxy `<img>` and still forbids the third-party host. Change its name too.

Replace this block (currently `validate.route.test.ts:16-36`):

```ts
  it('renders the earliest validation card with top candidate + alternates (text only)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 1 }] }); // getOwnedBatch
    pool.enqueue({ rows: [{                                    // next card
      id: 'c1', card_ref_id: 'base1-4', finish: null, finish_needs_confirmation: true,
      confidence: 62, status: 'validation', crop_index: 0,
      candidates: [{ card_ref_id: 'base1-4', score: 0.62 }, { card_ref_id: 'base1-2', score: 0.5 }],
    }] });
    pool.enqueue({ rows: [                                     // candidate ref names
      { id: 'base1-4', name: 'Charizard', set_name: 'Base', number: '4', finishes: ['holo'] },
      { id: 'base1-2', name: 'Blastoise', set_name: 'Base', number: '2', finishes: ['holo'] },
    ] });
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/validate');
    expect(res.status).toBe(200);
    expect(res.text).toContain('/img/crop/c1');   // user's own crop as an image
    expect(res.text).toContain('Charizard');
    expect(res.text).toContain('Blastoise');
    expect(res.text).not.toContain('images.pokemontcg.io'); // Assembly Resolution 9: no external ref image
    expect(res.text).toContain('name="finish"'); // finish selector shown (needs_confirmation)
  });
```

with:

```ts
  it('renders the earliest validation card with reference thumbnails via the /img/ref proxy', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 1 }] }); // getOwnedBatch
    pool.enqueue({ rows: [{                                    // next card
      id: 'c1', card_ref_id: 'base1-4', finish: null, finish_needs_confirmation: true,
      confidence: 62, status: 'validation', crop_index: 0,
      candidates: [{ card_ref_id: 'base1-4', score: 0.62 }, { card_ref_id: 'base1-2', score: 0.5 }],
    }] });
    pool.enqueue({ rows: [                                     // candidate ref names
      { id: 'base1-4', name: 'Charizard', set_name: 'Base', number: '4', finishes: ['holo'] },
      { id: 'base1-2', name: 'Blastoise', set_name: 'Base', number: '2', finishes: ['holo'] },
    ] });
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/validate');
    expect(res.status).toBe(200);
    expect(res.text).toContain('/img/crop/c1');       // user's own crop as an image
    expect(res.text).toContain('Charizard');
    expect(res.text).toContain('Blastoise');
    // M3: candidates now carry a reference thumbnail served by the local proxy.
    expect(res.text).toContain('/img/ref/base1-4');   // top candidate ref image (proxy)
    expect(res.text).toContain('/img/ref/base1-2');   // alternate ref image (proxy)
    // CSP still holds: the image src is the same-origin proxy, never the 3rd-party host,
    // and there are no inline event handlers (onerror etc).
    expect(res.text).not.toContain('images.pokemontcg.io');
    expect(res.text).not.toContain('onerror');
    expect(res.text).toContain('name="finish"'); // finish selector shown (needs_confirmation)
  });
```

- [ ] **Step 2: Run the test to verify it fails**

Run (Node 20 on PATH):
```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run tests/validate.route.test.ts
```
Expected: FAIL — the new test's `expect(res.text).toContain('/img/ref/base1-4')` fails because
the template still renders candidates as text only.

- [ ] **Step 3: Add the reference thumbnail to the partial**

Edit `web/views/partials/validate-card.njk`. Remove the Assembly-Resolution-9 "text only"
comment and add an `<img>` inside each candidate `<label>`, sourced from the proxy. The full
updated file:

```njk
<div class="validate-card" data-card-id="{{ card.id }}">
  <img class="crop" src="/img/crop/{{ card.id }}" alt="detected card crop">
  {# M3 amendment #1: reference art is served by the local /img/ref proxy (from MinIO),
     so the CSP stays 'self'+MinIO. No inline onerror (CSP-forbidden); a proxy 404 just
     shows the browser's broken-image glyph, which is acceptable. #}
  <form class="validate-form" method="post" action="/cards/{{ card.id }}/validate">
    <fieldset class="candidates">
      <legend>Which card is this?</legend>
      {% for opt in options %}
        <label class="candidate">
          <input type="radio" name="card_ref_id" value="{{ opt.id }}"
                 {% if loop.first %}checked{% endif %} data-index="{{ loop.index }}">
          <img class="ref-thumb" src="/img/ref/{{ opt.id }}" alt="reference card art" width="120">
          <span>{{ opt.name }} — {{ opt.set_name }} #{{ opt.number }}</span>
        </label>
      {% endfor %}
    </fieldset>

    {% if card.finish_needs_confirmation %}
      <fieldset class="finish">
        <legend>Finish</legend>
        <label><input type="radio" name="finish" value="non-holo" checked> Non-holo</label>
        <label><input type="radio" name="finish" value="reverse"> Reverse</label>
        <label><input type="radio" name="finish" value="holo"> Holo</label>
      </fieldset>
    {% endif %}

    <div class="search">
      <input type="text" name="q" placeholder="Search all cards…" autocomplete="off"
             hx-get="/api/search-refs" hx-trigger="keyup changed delay:300ms"
             hx-target="#search-results" hx-swap="innerHTML">
      <div id="search-results"></div>
    </div>

    <div class="actions">
      <button type="submit" name="_action" value="confirm">Confirm (Enter)</button>
    </div>
  </form>

  <form method="post" action="/cards/{{ card.id }}/skip" class="inline">
    <button type="submit">Skip — unreadable (s)</button>
  </form>
  <form method="post" action="/cards/{{ card.id }}/not-card" class="inline">
    <button type="submit">Not a card (n)</button>
  </form>
</div>
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run tests/validate.route.test.ts
```
Expected: PASS — the reference-thumbnail test and the unchanged POST/skip/not-card tests all
green.

- [ ] **Step 5: Run the full web unit suite + typecheck**

Run:
```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run && pnpm typecheck
```
Expected: PASS — whole vitest suite green (E2E specs skip without `E2E=1`), zero TS errors
(this task changed only a `.njk` template and a test).

- [ ] **Step 6: Commit**

```bash
git add web/views/partials/validate-card.njk web/tests/validate.route.test.ts
git commit -m "feat(web): reference thumbnails in validation UI via proxy"
```

---

### Task 10: E2E pricing loop + finish narrowing + finisher

This is the M3 acceptance gate and the milestone finisher. It extends the E2E harness with a new
spec that drives the pricing flow end to end against real Postgres + MinIO and a real worker
subprocess, then wraps up M3: it adds the test-only `NOTBULK_STUB_PRICE` seam to
`handlers/price.py`, documents M3 in the repo runbook, and bumps `VERSION` to `0.4.0`.

A **new** file `web/tests/e2e/pricing.e2e.test.ts` keeps M3's concerns separate from the M2 loop
spec. It is `E2E=1`-gated (skipped otherwise) and self-cleaning in `afterAll`.

**Ref-image seeding (avoids a real pokemontcg.io fetch):** the spec pre-seeds
`card_refs.ref_cached_key` AND puts a real object at that MinIO key, so `GET /img/ref/:id` 302s
from the cache without any network fetch. This is the SIMPLEST of the options and keeps the test
hermetic.

**Price seam:** the worker's `handle_price` (Task 3) hits pokemontcg.io. This task adds a 5-line
`NOTBULK_STUB_PRICE` env seam to `handlers/price.py`: when set, `handle_price` resolves a canned
price `(1234, 'pokemontcg')` instead of calling `resolve_price`. Inert when unset; documented
test-only.

**Files:**
- Create: `web/tests/e2e/pricing.e2e.test.ts`
- Modify: `worker/notbulk/handlers/price.py` (add the `NOTBULK_STUB_PRICE` seam near the top of
  `handle_price`)
- Modify: `worker/tests/test_handler_price.py` (add one test asserting the seam upserts the
  canned price without calling `resolve_price` — parallels the identify-stub coverage)
- Modify: `CLAUDE.md` (add M3 notes to the "Running M2 locally" runbook)
- Modify: `VERSION` (`0.3.0` → `0.4.0`)

**Interfaces:**
- Consumes:
  - `handle_price(pool, storage, payload, cfg)` (Task 3) — payload `{card_ref_id, finish}`;
    reads cache, resolves via configured sources, upserts, then calls
    `maybe_narrow_finish_flag` (Task 8).
  - `resolve_price(sources, card_ref_id, finish, cfg)` (Task 2) — returns
    `(cents_or_None, source_name)`. The seam replaces this call, so `handle_price` must build the
    canned tuple in the exact `(1234, 'pokemontcg')` shape `resolve_price` returns.
  - `NOTBULK_STUB_IDENTIFY=1` / `NOTBULK_STUB_REF_ID` (M2 seam, `handlers/identify.py`) — used to
    drive identify offline in the E2E spec (identity resolves to the seeded ref).
  - `getCollection` / `getCollectionStats` (Task 6) + `GET /collection`,
    `GET /collection/export.csv` (Tasks 6, 7) — asserted by the spec.
  - `GET /img/ref/:cardRefId` (Task 5) — asserted to 302 from the pre-seeded MinIO cache.
- Produces:
  - `NOTBULK_STUB_PRICE` env seam in `handlers/price.py` (test-only; inert when unset).
  - The M3 acceptance spec `web/tests/e2e/pricing.e2e.test.ts`.
  - `VERSION` = `0.4.0` and the M3 runbook notes (milestone finisher).

- [ ] **Step 1: Add the `NOTBULK_STUB_PRICE` seam to `handle_price` — write the worker test first**

Add a test to `worker/tests/test_handler_price.py` (created in Task 3). It sets the env var,
patches `resolve_price` to explode if called, and asserts the canned price `1234` /
`'pokemontcg'` was upserted. Use the scripted-pool pattern already in that file (mirror
`test_handler_finish.py`'s `ScriptedPool` if Task 3 didn't add one — the responder must answer
the `read_cached` SELECT with a stale/absent result so the resolve path is taken).

```python
def test_stub_price_seam_upserts_canned_without_resolving(monkeypatch):
    """NOTBULK_STUB_PRICE=1: handle_price upserts (1234,'pokemontcg') for the
    payload finish WITHOUT calling resolve_price (the network path). Inert-when-
    unset behavior is covered by the non-stub tests in this file."""
    from notbulk.handlers import price as ph

    monkeypatch.setenv("NOTBULK_STUB_PRICE", "1")

    def _boom(*a, **kw):
        raise AssertionError("resolve_price must NOT be called under NOTBULK_STUB_PRICE")

    monkeypatch.setattr(ph, "resolve_price", _boom)
    # No-op the downstream narrow so this test isolates the price upsert.
    monkeypatch.setattr(ph, "maybe_narrow_finish_flag", lambda pool, cid, cfg: None)

    upserts = []
    monkeypatch.setattr(
        ph, "upsert_price",
        lambda pool, cid, fin, cents, src: upserts.append((cid, fin, cents, src)),
    )
    # read_cached returns (fresh=False, None) so the resolve/upsert path runs.
    monkeypatch.setattr(ph, "read_cached", lambda pool, cid, fin, ttl: (False, None))

    cfg = {"pricing": {"cache_ttl_hours": 24, "finish_spread_flag_pct": 15,
                       "source_order": ["pokemontcg"]}}
    pool = object()
    ph.handle_price(pool, object(), {"card_ref_id": "base1-4", "finish": "normal"}, cfg)

    assert upserts == [("base1-4", "normal", 1234, "pokemontcg")]
```

- [ ] **Step 2: Run the worker test to verify it fails**

Run:
```bash
cd worker && uv run pytest tests/test_handler_price.py::test_stub_price_seam_upserts_canned_without_resolving -v
```
Expected: FAIL — either an `AssertionError` from `_boom` (the seam isn't there, so
`resolve_price` is still called) or an `AttributeError` if the referenced attributes differ;
either way the canned-upsert assertion does not hold.

- [ ] **Step 3: Add the seam to `handle_price`**

Edit `worker/notbulk/handlers/price.py`. Add the seam immediately after `read_cached` reports
the price is not fresh and before `resolve_price` is called — so the cache-hit short-circuit
(Task 3) still wins, and the seam only replaces the network resolve. Add `import os` at the top
if Task 3 didn't already. The seam block (drop it in place of / directly guarding the
`resolve_price` call):

```python
        # Test-only seam: deterministic offline pricing for the M3 E2E loop.
        # When NOTBULK_STUB_PRICE is set, skip the network resolve and use a canned
        # price so the queue/cache/narrow/explorer path can be exercised without
        # hitting pokemontcg.io. Never set in production. (Mirrors NOTBULK_STUB_IDENTIFY.)
        if os.environ.get("NOTBULK_STUB_PRICE") == "1":
            cents, source = 1234, "pokemontcg"
        else:
            cents, source = resolve_price(sources, card_ref_id, finish, cfg)
```

(Adjust the surrounding names to Task 3's `handle_price` — `sources`, `card_ref_id`, `finish`,
`cfg` are the contract names. The seam must NOT bypass `read_cached` and MUST still be followed
by `upsert_price(...)` and `maybe_narrow_finish_flag(...)` exactly as the non-stub path is.)

- [ ] **Step 4: Run the worker test to verify it passes + full worker suite**

Run:
```bash
cd worker && uv run pytest tests/test_handler_price.py::test_stub_price_seam_upserts_canned_without_resolving -v
cd worker && uv run pytest tests ../eval/tests
```
Expected: PASS — the seam test green; the whole worker + eval suite green (the seam is inert when
the env var is unset, so no other test changes behavior).

- [ ] **Step 5: Write the E2E spec**

Create `web/tests/e2e/pricing.e2e.test.ts`. It mirrors `loop.e2e.test.ts`'s structure (real
pool, real worker subprocess, `waitFor`, self-cleaning `afterAll`) but seeds a card_ref with two
finishes and a pre-cached MinIO ref object, runs the worker with BOTH stub seams, and asserts the
full pricing surface.

```ts
// M3 acceptance gate: identify -> price (stub) -> finish-narrow -> explorer/CSV,
// against REAL local services (Postgres 5434, MinIO 9000) and a REAL worker
// subprocess. Gated on E2E=1 so the normal `pnpm vitest run` skips it.
//
// The worker runs with NOTBULK_STUB_IDENTIFY=1 (canned 'h'-stage id to the seeded
// ref) AND NOTBULK_STUB_PRICE=1 (canned 1234c 'pokemontcg' price for every finish),
// so this test exercises the real job queue, price cache, finish-spread narrowing,
// and the collection explorer/CSV without any pokemontcg.io network dependency.
//
// Ref image: card_refs.ref_cached_key is pre-seeded to a real MinIO object, so
// GET /img/ref/:id 302s from the cache with no proxy fetch.
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawn, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Pool } from 'pg';
import { uuidv7 } from 'uuidv7';
import { createHash } from 'node:crypto';
import request from 'supertest';
import { createApp } from '../../src/app.js';
import { loadConfig } from '../../src/config.js';
import { getPool } from '../../src/db.js';
import { Storage } from '../../src/services/storage.js';
import { sessionMiddleware } from '../../src/middleware/session.js';

const RUN = process.env.E2E === '1';
const d = RUN ? describe : describe.skip;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIX = path.join(__dirname, 'fixtures');
const REF_ID = 'e2e-price-base1-4';
const REF_KEY = `refs/${REF_ID}.webp`;

async function waitFor<T>(fn: () => Promise<T | null>, timeoutMs: number): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('waitFor timed out');
}

d('M3 e2e pricing loop (real Postgres + MinIO + stubbed worker)', () => {
  let pool: Pool;
  let worker: ChildProcess;
  let userId: string;
  let token: string;
  let seededBatchIds: string[] = [];

  beforeAll(async () => {
    pool = getPool();
    const cfg = loadConfig();
    const storage = new Storage(cfg);

    // Card ref with TWO finishes -> identify forces finish_needs_confirmation + validation,
    // and the equal stub prices (spread 0%) let the narrow clear the flag to 'auto'.
    // Pre-seed ref_cached_key + a real MinIO object so /img/ref 302s with no fetch.
    await storage.put(REF_KEY, Buffer.from('webp-ref-bytes'), 'image/webp').catch(() => {});
    await pool.query(
      `INSERT INTO card_refs (id, name, set_id, set_name, number, image_url, finishes, ref_cached_key)
         VALUES ($1,'E2E Price Card','e2e-set','E2E Set','4',
                 'https://images.pokemontcg.io/e2e/4.png', ARRAY['normal','holofoil'], $2)
       ON CONFLICT (id) DO UPDATE SET ref_cached_key=EXCLUDED.ref_cached_key,
                                      finishes=EXCLUDED.finishes`,
      [REF_ID, REF_KEY],
    );

    userId = uuidv7();
    await pool.query(`INSERT INTO users (id, email, tier) VALUES ($1,$2,'free')`, [
      userId, `e2e-price-${userId}@test.local`,
    ]);
    const raw = uuidv7();
    token = raw;
    const tokenHash = createHash('sha256').update(raw).digest('hex');
    await pool.query(
      `INSERT INTO sessions (id, user_id, token_hash, expires_at)
         VALUES ($1,$2,$3, now() + interval '30 days')`,
      [uuidv7(), userId, tokenHash],
    );

    // Worker with BOTH stub seams: offline identify -> the seeded ref, offline price.
    worker = spawn('uv', ['run', 'notbulk-worker'], {
      cwd: path.resolve(__dirname, '../../../worker'),
      env: {
        ...process.env,
        NOTBULK_STUB_IDENTIFY: '1',
        NOTBULK_STUB_REF_ID: REF_ID,
        NOTBULK_STUB_PRICE: '1',
      },
      stdio: 'inherit',
    });
    await new Promise((r) => setTimeout(r, 2000)); // let it connect + LISTEN
  }, 30_000);

  afterAll(async () => {
    if (worker) worker.kill('SIGTERM');
    const cfg = loadConfig();
    const storage = new Storage(cfg);
    for (const batchId of seededBatchIds) {
      const photos = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [batchId]);
      const cards = await pool.query(
        `SELECT crop_storage_key FROM cards c JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`,
        [batchId],
      );
      for (const p of photos.rows) if (p.storage_key) await storage.delete(p.storage_key).catch(() => {});
      for (const c of cards.rows) if (c.crop_storage_key) await storage.delete(c.crop_storage_key).catch(() => {});
    }
    await storage.delete(REF_KEY).catch(() => {});
    await pool.query(`DELETE FROM prices WHERE card_ref_id=$1`, [REF_ID]);
    await pool.query(`DELETE FROM ref_hashes WHERE card_ref_id=$1`, [REF_ID]);
    await pool.query(`DELETE FROM users WHERE id=$1`, [userId]); // cascades sessions/batches/photos/cards/jobs
    await pool.query(`DELETE FROM card_refs WHERE id=$1`, [REF_ID]);
    await pool.end();
  });

  it('identify -> price -> narrow -> explorer + CSV + ref proxy', async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool,
      cfg,
      storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    // 1. Create a batch with one fixture photo.
    const create = await request(app)
      .post('/batches')
      .set('Cookie', `nb_session=${token}`)
      .attach('photos', path.join(FIX, 'card-a.jpg'));
    expect(create.status).toBe(302);
    const batchId = create.headers.location.split('/').pop()!;
    seededBatchIds.push(batchId);

    // 2. Wait for the batch to complete (identify done).
    await waitFor(async () => {
      const r = await pool.query(`SELECT status FROM batches WHERE id=$1`, [batchId]);
      return r.rows[0]?.status === 'complete' ? true : null;
    }, 60_000);

    // 3. Price rows exist for BOTH finishes of the ref (identify enqueues a price
    //    job per finish; the stub upserts 1234c each). Wait for both.
    await waitFor(async () => {
      const r = await pool.query(
        `SELECT count(*)::int AS n FROM prices WHERE card_ref_id=$1 AND price_cents IS NOT NULL`,
        [REF_ID],
      );
      return Number(r.rows[0].n) >= 2 ? true : null;
    }, 30_000);
    const priced = await pool.query(
      `SELECT finish, price_cents, source FROM prices WHERE card_ref_id=$1 ORDER BY finish`,
      [REF_ID],
    );
    expect(priced.rows.map((p) => p.finish).sort()).toEqual(['holofoil', 'normal']);
    expect(priced.rows.every((p) => p.price_cents === 1234 && p.source === 'pokemontcg')).toBe(true);

    // 4. Finish-narrowing ran: equal prices => 0% spread <= 15% => flag cleared,
    //    the card moved from 'validation' to 'auto'. Wait for the narrow.
    const card = await waitFor(async () => {
      const r = await pool.query(
        `SELECT c.id, c.status, c.finish, c.finish_needs_confirmation
           FROM cards c JOIN photos p ON p.id=c.photo_id
          WHERE p.batch_id=$1 LIMIT 1`,
        [batchId],
      );
      const row = r.rows[0];
      return row && row.status === 'auto' && row.finish_needs_confirmation === false ? row : null;
    }, 30_000);
    // Narrowed finish is the first FINISH_KEYS-order key present = 'normal'.
    expect(card.finish).toBe('normal');

    // 5. GET /collection renders the priced card as $12.34.
    const coll = await request(app).get('/collection').set('Cookie', `nb_session=${token}`);
    expect(coll.status).toBe(200);
    expect(coll.text).toContain('$12.34');

    // 6. GET /collection/export.csv contains the priced row (name + $12.34).
    const csv = await request(app).get('/collection/export.csv').set('Cookie', `nb_session=${token}`);
    expect(csv.status).toBe(200);
    expect(csv.headers['content-type']).toContain('text/csv');
    expect(csv.text).toContain('$12.34');
    expect(csv.text).toContain('E2E Price Card');

    // 7. GET /img/ref/:id 302s from the pre-seeded MinIO cache (no proxy fetch).
    const ref = await request(app).get(`/img/ref/${REF_ID}`).set('Cookie', `nb_session=${token}`);
    expect(ref.status).toBe(302);
    expect(ref.headers.location).toContain('127.0.0.1:9000'); // signed MinIO URL, not pokemontcg.io
  }, 180_000);
});
```

- [ ] **Step 6: Run the E2E spec once with `E2E=1` (single command)**

Bring up services + migrations, then run ONLY this spec with both stub seams. The spec spawns the
worker itself in `beforeAll`.

```bash
docker compose up -d
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 \
  DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  pnpm vitest run tests/e2e/pricing.e2e.test.ts
```
Expected: PASS — `1 passed` for `M3 e2e pricing loop`; the worker subprocess logs on stdout
(`stdio: 'inherit'`), the batch completes, both price rows appear, the card narrows to
`auto`/`normal`, `/collection` and the CSV show `$12.34`, and `/img/ref/:id` returns `302` to a
`127.0.0.1:9000` signed URL. The test self-cleans in `afterAll`.

- [ ] **Step 7: Commit the E2E spec + the worker seam**

```bash
git add worker/notbulk/handlers/price.py worker/tests/test_handler_price.py web/tests/e2e/pricing.e2e.test.ts
git commit -m "test(e2e): M3 pricing loop with stubbed price seam"
```

- [ ] **Step 8: FINISHER — update the runbook, bump VERSION, run all gates**

Edit `CLAUDE.md`: extend the "Running M2 locally" section with M3 notes. Add this subsection
immediately after the existing `### M2 end-to-end loop test` block (after line 104):

```markdown
### M3 pricing + collection explorer

Pricing flows automatically: the identify handler enqueues one `price` job per finish of the
resolved card; the worker's `price` handler fetches from pokemontcg.io (keyless works at low
volume; `POKEMONTCG_API_KEY` raises the limit), caches into `prices` as integer cents (NULL =
cached known-miss, never `$0`), and then runs the finish-spread narrowing. The web layer only
READS the cache:

- `GET /collection` — the explorer grid (sort/filter/stats), owner-scoped.
- `GET /collection/export.csv` — streamed RFC-4180 CSV, one row per `auto`/`validated`/`corrected` card.
- `GET /img/ref/:cardRefId` — reference-art proxy; caches `images.pokemontcg.io` art into MinIO
  once, then 302s to a signed MinIO URL (CSP stays `self`+MinIO).

**Test-only seam:** `NOTBULK_STUB_PRICE=1` makes the worker's `price` handler return a canned
`1234c` / `pokemontcg` price instead of hitting pokemontcg.io (mirrors `NOTBULK_STUB_IDENTIFY`).
Inert when unset; never set in production. Used by the M3 E2E spec below.

### M3 end-to-end pricing test

`web/tests/e2e/pricing.e2e.test.ts` drives identify -> price -> finish-narrow -> explorer/CSV
against real local Postgres + MinIO with a real worker subprocess (both stub seams:
`NOTBULK_STUB_IDENTIFY=1` + `NOTBULK_STUB_PRICE=1`). Gated on `E2E=1`, skipped otherwise.
Single-command form (the test spawns the worker itself in `beforeAll` and pre-seeds the MinIO
ref object so `/img/ref` needs no network fetch):

    docker compose up -d
    DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
    cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 \
      DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      pnpm vitest run tests/e2e/pricing.e2e.test.ts

The test self-cleans (deletes its seeded rows, prices, and MinIO objects in `afterAll`).
```

Then bump `VERSION`:

```
0.4.0
```

- [ ] **Step 9: Run every gate green before committing the finisher**

Run all four gates. All must pass (the E2E once with `E2E=1`; the rest with it unset so E2E
skips).

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run          # unit suite: unpriced/priced explorer + csv + refproxy tests; E2E skips
cd web && pnpm typecheck           # zero TS errors
cd worker && uv run pytest tests ../eval/tests   # pricing + finish + price-handler tests; eval still green
```
Expected:
- `pnpm vitest run` — all web unit specs pass, E2E specs report skipped.
- `pnpm typecheck` — no errors.
- `pytest tests ../eval/tests` — all worker + eval tests pass (eval regression harness green; the
  finish-narrowing invariant tests included).

Then the single E2E invocation once (from Step 6) shows `1 passed`.

- [ ] **Step 10: Commit the finisher**

```bash
git add CLAUDE.md VERSION
git commit -m "docs(m3): pricing runbook notes; chore: bump VERSION to 0.4.0"
```
