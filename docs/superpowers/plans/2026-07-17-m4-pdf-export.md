# M4 — PDF Export + Discord Hooks + HEIC + Config Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user can export their collection as a print-styled PDF (rendered async, downloaded via a signed URL), upload HEIC photos (converted server-side), and the operator gets Discord notifications on pipeline errors and batch completion — all local on MINTY.

**Architecture:** PDF rendering is a NEW `export` job type drained by a NEW Node export worker (Puppeteer) — the Postgres job queue's claim becomes type-partitioned so the Python pipeline worker and the Node export worker never claim each other's jobs. Crops are embedded as base64 data URIs so the headless browser needs no network or auth (defense-in-depth). HEIC is a Node upload-gate change only (decoded by heic-convert since sharp cannot; the worker never sees HEIC because uploads are re-encoded to WebP before the queue). A shared Python Discord notifier posts sanitized events.

**Tech Stack:** unchanged base (Python 3.11/uv worker, Node 20/pnpm web, Postgres 5434, MinIO 9000) + puppeteer (Node; system google-chrome at /usr/bin/google-chrome and chrome-headless-shell already present). HEIC decodes via heic-convert (WASM) — sharp cannot decode HEVC despite reporting heif input support. sharp re-encodes the resulting JPEG to WebP. No new Python deps (httpx covers Discord; HEIC never reaches Python).

## Global Constraints

- Secrets ONLY via `bws run`. `DISCORD_WEBHOOK_URL` (existing M1 secret name) from env; never printed/committed; a config toggle `discord.enabled` gates all posting; when the env is unset the notifier is a no-op (logs a warning once).
- **Zero wrong auto-accepts unchanged.** M4 adds NO status-mutating pipeline logic; the eval suite (`cd worker && uv run python ../eval/regression.py`) must still pass (exit 2 acceptable while ref_hashes empty).
- **PDF security (design S8):** Puppeteer runs with JavaScript DISABLED (`page.setJavaScriptEnabled(false)`); crops embedded as base64 `data:` URIs (the browser makes ZERO network requests — no signed-URL/auth exposure inside Chromium); ALL user-derived strings (card names from OCR/LLM, `batches.origin_url`, any free text) are context-escaped by the template's autoescaping; render is concurrency-1 and bounded by `export.render_timeout_ms`; the browser is launched with a sandbox (note: on the eventual VPS a seccomp/userns sandbox compensates if `--no-sandbox` is needed in a container — M5 concern; locally the default sandbox works as a non-root user).
- **Every export carries the non-affiliation disclaimer** (spec §10.6): the PDF footer includes VERBATIM `NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.` (note the é in Pokémon).
- **HEIC hardening (design S7):** HEIC is the highest-risk decode path (libheif/libde265). The gate keeps every existing limit (byte cap, `limitInputPixels` = 50 MP, no-throw guarantee) and adds HEIC magic-byte detection (ISO-BMFF `ftyp` with a HEIF brand). A crafted malformed HEIC must REJECT via a GateReject, never throw out of `gateImage` (AC-8). HEIC decodes via the pinned heic-convert (WASM); sharp re-encodes the resulting JPEG to WebP.
- **Async export + retention (design Q13/Q20):** exports are async jobs; the artifact lands in MinIO under `exports/{user_id}/{export_id}.pdf` with a retention `expires_at` (config `export.retention_hours`, default 48); download is an owner-checked signed URL; a past-`expires_at` export returns 410. (Physical GC of expired export objects is the M5 janitor — M4 only sets `expires_at` and refuses expired downloads.)
- **Ownership scoping (AC 7):** every export create/status/download query filters by `user_id`; no bare-id path.
- **Queue partition invariant:** after M4 the `claim` SQL takes an allowed-types list. The Python pipeline worker claims its handler types (`detect,identify,fetch_source,ingest_correction,price`); the Node export worker claims ONLY `export`. No worker may claim a type it has no handler for (a claimed-but-unhandled job would dead-letter).
- Discord messages are SANITIZED (design S10): error CLASS + job/batch/card IDs, never a raw traceback with interpolated user content (filenames, OCR text); truncate long fields.
- Money is integer cents; the PDF/CSV reuse the shared `formatCents`. All IDs uuidv7. Conventional commits; VERSION → 0.5.0 ONLY in the final task.
- Web tests: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run`. Worker tests: `cd worker && uv run pytest tests ../eval/tests`. Structured image fixtures only.
- dbmate: `DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up`.
- **Out of scope for M4** (do not build): the nightly janitor / price-cache GC / orphan sweep / storage-recompute (all M5 per spec §12); Cloudflare/WAF/Turnstile-prod/nftables/VPS (M5); graded pricing (V2); per-user LLM sub-caps (M5).

## File Structure

```
migrations/004_m4_exports.sql                              # Task 1

worker/notbulk/
├── discord.py            # notify(cfg, level, title, fields) — sanitized Discord webhook       Task 2
├── jobqueue.py           # MODIFY: claim(pool, worker_id, allowed_types) type-partition        Task 3
└── worker.py             # MODIFY: claim its handler types; Discord on fail + batch-complete    Tasks 2,3

web/src/
├── services/imagegate.ts # MODIFY: accept HEIC (ftyp sniff) -> heic-convert -> WebP           Task 4
├── lib/pdf.ts            # renderCollectionPdf(rows, stats, opts) -> Buffer (Puppeteer)          Task 6
├── views/collection-pdf.njk # print-styled HTML: cover stats + crop grid + footer+disclaimer    Task 5
├── export-worker/
│   ├── jobqueue.ts       # Node claim/complete/fail (type='export'), mirrors the Python SQL      Task 7
│   └── worker.ts         # export worker loop: claim -> render -> upload -> exports row ready     Task 7
├── queries/exports.ts    # createExport / getOwnedExport / markReady / markFailed (owner-scoped)  Task 7
├── routes/exports.ts     # POST /collection/export.pdf, GET /collection/exports/:id[/download]    Task 8
└── views/export-status.njk # queued/rendering/ready/failed status page                           Task 8
```

## Interface Contract (authoritative — all tasks conform exactly)

### Migration 004 (dbmate up/down)

```sql
-- migrate:up
CREATE TABLE exports (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL DEFAULT 'pdf' CHECK (kind IN ('pdf')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','rendering','ready','failed')),
  storage_key text,                     -- NULL until ready
  card_count int NOT NULL DEFAULT 0,
  bytes bigint NOT NULL DEFAULT 0,
  last_error text,
  expires_at timestamptz,               -- set when ready = now() + retention
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX exports_user_idx ON exports (user_id, created_at DESC);

ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price','export'));

-- migrate:down
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));
DROP TABLE exports;
```

### config.yaml additions (Task 1)

```yaml
export:
  retention_hours: 48
  render_timeout_ms: 30000
  page_size: "Letter"
  storage_prefix: "exports"
  max_cards: 5000                       # bound the render; larger collections truncate with a note
discord:
  enabled: false                        # true once DISCORD_WEBHOOK_URL is provisioned; env-gated regardless
  timeout_seconds: 5
upload:
  accept_heic: true                     # M4: HEIC now accepted (heic-convert WASM); JPEG/PNG still accepted
```

### `job type` payloads (extend the M2/M3 contract)

| type | payload | enqueued by | handled by |
|---|---|---|---|
| `export` | `{"export_id": "<uuid>"}` | POST /collection/export.pdf (Node) | Node export-worker |

Claim partition: Python `worker.py` calls `jobqueue.claim(pool, worker_id, allowed_types=("detect","identify","fetch_source","ingest_correction","price"))`; Node export-worker claims `type = 'export'`. The `claim` SQL adds `AND type = ANY(%s)` to its inner SELECT.

### Python signatures

```python
# notbulk/discord.py
def notify(cfg: dict, level: str, title: str, fields: dict[str, str]) -> None:
    ...  # if not cfg['discord']['enabled'] or DISCORD_WEBHOOK_URL unset -> no-op (warn once).
         # POST a Discord embed to DISCORD_WEBHOOK_URL (httpx, timeout cfg.discord.timeout_seconds).
         # level in ('error','info'); fields VALUES are sanitized+truncated (<=1000 chars, no raw
         # tracebacks — callers pass error CLASS + ids, not str(exc) of a deep trace). Network/HTTP
         # failure is swallowed (a dead webhook must never crash the worker).

# notbulk/jobqueue.py  (MODIFY)
def claim(pool, worker_id: str, allowed_types: tuple[str, ...]) -> tuple[str, str, dict] | None:
    ...  # inner SELECT gains `AND type = ANY(%s)` bound to list(allowed_types); everything else unchanged.
```

Worker Discord wiring (Task 2): on terminal job failure (`fail(dead=True)` path) → `notify(cfg,'error','pipeline job failed',{'type':type,'job_id':id,'batch_id':...,'error_class':exc.__class__.__name__})`. On batch completion (identify handler's batch_complete transition — pass the notify through, or the worker observes the NOTIFY) → `notify(cfg,'info','batch complete',{'batch_id':...,'cards':...})`. Keep the identify handler pure; the batch-complete Discord post is emitted by the worker loop when it sees the `batch_complete` progress event OR by a small hook in the completion UPDATE path — the Task will pick the cleaner site and document it.

### Node signatures

```ts
// src/lib/pdf.ts
export interface PdfCard { cropDataUri: string | null; name: string; set: string; number: string; finish: string; priceDisplay: string; quantity: number }
export interface PdfStats { totalCards: number; totalValueDisplay: string; generatedAt: string }
export async function renderCollectionPdf(cards: PdfCard[], stats: PdfStats, cfg: Config): Promise<Buffer>;
  // Nunjucks-render collection-pdf.njk (autoescaped) with cards+stats -> HTML; launch puppeteer
  // (JS disabled, sandbox on), page.setContent(html, {waitUntil:'load'}), page.pdf({format:cfg.export.page_size,
  // printBackground:true}) -> Buffer. Bounded by cfg.export.render_timeout_ms. Concurrency-1 (a module-level
  // mutex/queue). Browser ALWAYS closed in finally. Throws on timeout/launch failure (caller marks the export failed).

// src/queries/exports.ts  (all owner-scoped where a userId is given)
export async function createExport(pool: Pool, userId: string, kind: string): Promise<string>;           // INSERT queued, RETURNING id
export async function getOwnedExport(pool: Pool, userId: string, exportId: string): Promise<ExportRow | null>;
export async function claimExportRow(pool: Pool, exportId: string): Promise<ExportRow | null>;            // worker-side, sets status='rendering'
export async function markExportReady(pool: Pool, exportId: string, storageKey: string, bytes: number, cardCount: number, expiresAt: Date): Promise<void>;
export async function markExportFailed(pool: Pool, exportId: string, error: string): Promise<void>;

// src/export-worker/jobqueue.ts — Node claim mirroring the Python SQL, type='export' only
export async function claimExportJob(pool: Pool, workerId: string): Promise<{ id: string; payload: { export_id: string } } | null>;
export async function completeJob(pool: Pool, jobId: string): Promise<void>;
export async function failJob(pool: Pool, jobId: string, error: string, dead: boolean): Promise<string>;

// Routes (requireUser, owner-scoped):
//   POST /collection/export.pdf        -> createExport + enqueue 'export' job {export_id} + NOTIFY jobs_wake -> 302 /collection/exports/:id
//   GET  /collection/exports/:id       -> owned export -> render export-status.njk (queued/rendering/ready/failed; meta-refresh poll until terminal; download link when ready)
//   GET  /collection/exports/:id/download -> owned + status='ready' + not past expires_at -> 302 signed MinIO URL; expired -> 410; not ready -> 409
```

### Export worker behavior (Task 7)

`export-worker/worker.ts` main loop: LISTEN jobs_wake (dedicated pg client) + 5s fallback poll; `claimExportJob` → load the export row (`claimExportRow` sets 'rendering') → load the user's full collection (reuse `getCollectionForExport` from M3 with the export's user_id) → for each card fetch its crop from MinIO (`storage.get`) and base64-encode to a `data:image/webp;base64,...` URI (truncate to `export.max_cards`, log truncation) → `renderCollectionPdf` → `storage.put(exports/{user_id}/{export_id}.pdf, buf, 'application/pdf')` → `markExportReady(...expires_at=now+retention)` → `completeJob`. On any error → `markExportFailed` + `failJob(dead=true)` + Discord error. Entry point `notbulk-export-worker` in web package.json scripts. Reuses the M2 Storage class (add a `get(key)->Buffer` if not present — it is, Python side has it; Node Storage has put/signedGetUrl/delete, ADD `get`).

### HEIC gate (Task 4)

Extend `isSupportedImage` in imagegate.ts: in addition to JPEG/PNG, accept ISO-BMFF HEIF — bytes[4..8] === 'ftyp' AND the brand (bytes[8..12]) ∈ {'heic','heix','hevc','hevx','mif1','msf1','heim','heis','hevm','hevs'}. When `cfg.upload.accept_heic` is false, reject HEIC (config kill-switch). HEIC decode uses **`heic-convert`** (Assembly Resolution 1 — sharp's bundled libheif cannot decode HEVC): heif tag → `heicConvert({buffer, format:'JPEG', quality:0.92})` → JPEG → the existing `sharp(jpeg).rotate().webp()` pipeline. AC-8: a truncated/malformed HEIC that passes the sniff but fails to decode → heic-convert (or sharp) throws → caught → GateReject 'corrupt image' (never throws out). Tests use the REAL committed fixture `web/tests/fixtures/sample-card.heic` for a genuine decode-success assertion, plus a truncated slice and the accept_heic=false kill-switch.

### Amendments to prior design (authoritative for M4)

1. **HEIC is a Node-gate-only change** — HEIC is decoded by heic-convert (sharp cannot); the Python worker never receives HEIC (uploads are WebP by the time they hit the queue). No Python HEIC decode, no pyvips/pillow-heif.
2. **PDF rendering is a Node export worker** draining a type-partitioned queue — the `claim` SQL gains an allowed-types filter so the Python and Node workers coexist on one `jobs` table.
3. **Puppeteer uses base64 data-URI crops** (no browser network) — the design's "network access disabled or restricted to R2" (S8) is satisfied by embedding, which also removes the need to expose signed URLs or auth cookies to Chromium.
4. **The janitor is M5** (spec §12) — M4 sets `exports.expires_at` and refuses expired downloads but does NOT physically GC.

## Assembly Resolutions (authoritative — override any conflicting task prose)

1. **HEIC decode uses `heic-convert` (pure-JS WASM HEVC decoder), NOT sharp.** DISCOVERY during planning: sharp's bundled libheif reports `format.heif.input: true` but CANNOT decode HEVC-compressed HEIC ("Support for this compression format has not been built in") — a real HEIC upload would fail. Owner-approved fix: `pnpm add heic-convert` (+ `@types/heic-convert`; ~7 MB, bundles libde265 as WASM, NO system libraries). Task 4's gate path becomes: detect HEIC via the ISO-BMFF ftyp-brand sniff → `heicConvert({buffer, format:'JPEG', quality:0.92})` → JPEG buffer → the EXISTING `sharp(jpeg).rotate().webp()` pipeline (EXIF stripped, nothing raw stored — the gate invariant holds). Task 4's prose that says "sharp decodes HEIF natively" is SUPERSEDED. heic-convert runs inside the same try/catch so a malformed HEIC → GateReject, never throws (AC-8). Because heic-convert is a new dep in the highest-risk decode path (S7), keep it version-pinned.
2. **A REAL committed HEIC fixture exists**: `web/tests/fixtures/sample-card.heic` (1053 bytes, `heic`-branded, structured content, verified decodable by heic-convert → 8548-byte JPEG). Task 4 asserts GENUINE decode-success: gateImage(real HEIC) → ok WebP out (proves heic-convert+sharp actually decoded it), NOT just the ftyp sniff. Also assert: accept_heic=false → reject; a truncated/garbage HEIC → GateReject not throw; JPEG/PNG unaffected. Task 9's HEIC E2E leg uploads this same fixture and asserts a stored WebP photo.
3. **Batch-complete Discord post** lives at identify.py's existing single-fire `fired` guard (a pure additive `_notify_batch_complete(cfg, batch_id, fired)` call, inert when discord disabled) — NOT a new worker-side batch_progress LISTEN. Error-notify on terminal job failure carries `error_class` (class name only, never a raw trace) + `batch_id` from payload or 'n/a' (no DB lookup on the failure path).
3b. **Discord egress**: `discord.com` (the webhook host) is a NEW outbound destination introduced by M4. Task 2 creates/appends `infra/egress-manifest.md` (the running record M5's nftables allowlist is generated from) with a `discord.com | Discord webhook (errors, batch-complete, summaries) | 443` row. If `infra/egress-manifest.md` does not yet exist, Task 2 creates it with a one-line header and this row; it is a docs artifact, not code — no test, committed with Task 2.
4. **`claim(pool, worker_id, allowed_types)`** — allowed_types derived from `set(_build_handlers().keys())` with an in-code assertion that every allowed type has a handler; Python worker passes its 5 handler types, the Node export worker claims `type='export'` only. The three existing Python claim call sites updated.
5. **NOTBULK_STUB_PDF seam** is at the TOP of `renderCollectionPdf` in web/src/lib/pdf.ts (early-return a canned `%PDF-1.4` buffer when set, browser never launched) — so the export-worker→storage→routes E2E path is fully real with only Chromium stubbed. Test-only, inert unset. The real Puppeteer render is exercised once behind `PDF_RENDER=1`.
6. **Node export worker does NOT post to Discord on failure** (the Python discord.py isn't reachable from Node) — it `markExportFailed` (user sees 'failed' + last_error) + console.error. A Node Discord poster is an M5 follow-up. The export worker always fails DEAD (no retry — a rerun is a fresh export); `failJob(dead)` keeps the param for signature parity.
7. **export-worker invocation**: `web/package.json` gains `"export-worker": "tsx src/export-worker/worker.ts"`; run via `cd web && pnpm export-worker`. `notbulk-export-worker` is the worker-id/log string, not an npm bin.
8. **Puppeteer** launches `headless:true` with the bundled chrome-headless-shell for determinism, falling back to `channel:'chrome'` (/usr/bin/google-chrome) on an executable-not-found launch error. No `--no-sandbox` locally (M5/VPS concern). JS disabled, data-URI crops only (zero browser network, S8).
9. **exports.expires_at** set to `now()+export.retention_hours` only when status→ready; download refuses past-expiry (410) and not-ready (409); physical GC of expired export objects is M5 (janitor).

---

<!-- TASK SECTIONS ASSEMBLED FROM PARALLEL DRAFTS BELOW -->
<!--
  M4 Part 1 — Tasks 1–3, assembled section of docs/superpowers/plans/2026-07-17-m4-pdf-export.md.
  Conforms to that plan's Global Constraints + Interface Contract (authoritative).
  Task Structure per superpowers:writing-plans (Files / Interfaces / checkbox steps,
  complete code every code step, exact commands + expected output, TDD, one conventional commit per task).
-->

### Task 1: Migration 004 + config additions

**Files:**
- Create: `migrations/004_m4_exports.sql`
- Modify: `config.yaml` (append `export:`, `discord:`, `upload:` blocks)
- Modify: `db/schema.sql` (regenerated by dbmate — do not hand-edit)

**Interfaces:**
- Consumes: existing `jobs` table with `jobs_type_check CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'))` (migration 002); `users(id)` (migration 002).
- Produces: `exports` table (columns per contract); `jobs_type_check` extended to include `'export'`; `config.yaml` keys `export.{retention_hours,render_timeout_ms,page_size,storage_prefix,max_cards}`, `discord.{enabled,timeout_seconds}`, `upload.accept_heic`. Later tasks read `cfg['discord']['enabled']` / `cfg['discord']['timeout_seconds']` (Task 2), and the migration lets Task 3 enqueue `type='export'` jobs.

- [ ] **Step 1: Write the migration file**

Create `migrations/004_m4_exports.sql` — DDL verbatim from the Interface Contract:

```sql
-- migrate:up
CREATE TABLE exports (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL DEFAULT 'pdf' CHECK (kind IN ('pdf')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','rendering','ready','failed')),
  storage_key text,                     -- NULL until ready
  card_count int NOT NULL DEFAULT 0,
  bytes bigint NOT NULL DEFAULT 0,
  last_error text,
  expires_at timestamptz,               -- set when ready = now() + retention
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX exports_user_idx ON exports (user_id, created_at DESC);

ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price','export'));

-- migrate:down
ALTER TABLE jobs DROP CONSTRAINT jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
  CHECK (type IN ('detect','identify','fetch_source','ingest_correction','price'));
DROP TABLE exports;
```

- [ ] **Step 2: Apply the migration**

Run (inline compose-local DSN — plaintext local creds from `docker-compose.yml`, not secret material; BWS dev token not yet provisioned on this box):

```bash
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
```

Expected (dbmate prints the applied version and writes the schema snapshot):

```
Applying: 004_m4_exports.sql
Writing: ./db/schema.sql
```

- [ ] **Step 3: Verify the exports table shape**

Run:

```bash
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate wait >/dev/null
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' -c '\d exports'
```

Expected — a table with columns `id uuid` (PK), `user_id uuid not null`, `kind text` (default `'pdf'`), `status text` (default `'queued'`), `storage_key text`, `card_count integer`, `bytes bigint`, `last_error text`, `expires_at timestamptz`, `created_at`/`updated_at timestamptz not null`, index `exports_user_idx` on `(user_id, created_at DESC)`, a FK `user_id -> users(id) ON DELETE CASCADE`, and CHECK constraints on `kind` and `status`.

- [ ] **Step 4: Verify the jobs CHECK now admits 'export'**

Run:

```bash
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' -c '\d jobs' | grep jobs_type_check
```

Expected (the constraint line lists all six types, `export` included):

```
    "jobs_type_check" CHECK (type = ANY (ARRAY['detect'::text, 'identify'::text, 'fetch_source'::text, 'ingest_correction'::text, 'price'::text, 'export'::text]))
```

- [ ] **Step 5: Probe that an 'export' job inserts and a bogus type is rejected**

Run (self-cleaning: the INSERT is rolled back, the bogus INSERT is expected to error inside its own transaction):

```bash
psql 'postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' -v ON_ERROR_STOP=0 <<'SQL'
BEGIN;
INSERT INTO jobs (id, type, payload) VALUES (gen_random_uuid(), 'export', '{"export_id":"00000000-0000-0000-0000-000000000000"}');
ROLLBACK;
BEGIN;
INSERT INTO jobs (id, type, payload) VALUES (gen_random_uuid(), 'bogus', '{}');
ROLLBACK;
SQL
```

Expected — the first block prints `INSERT 0 1` then `ROLLBACK`; the second block errors:

```
ERROR:  new row for relation "jobs" violates check constraint "jobs_type_check"
```

- [ ] **Step 6: Verify the down/up round-trip**

Run:

```bash
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate down
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
```

Expected — `down` prints `Rolling back: 004_m4_exports.sql` (drops `exports`, restores the 5-type CHECK); `up` re-applies `004` and rewrites `db/schema.sql`. After the round-trip `exports` exists again and the CHECK includes `export` (re-run Step 4 to confirm if desired).

- [ ] **Step 7: Append the config.yaml blocks**

Append to `config.yaml` (verbatim from the Interface Contract — add at end of file, top-level keys):

```yaml
export:
  retention_hours: 48
  render_timeout_ms: 30000
  page_size: "Letter"
  storage_prefix: "exports"
  max_cards: 5000                       # bound the render; larger collections truncate with a note
discord:
  enabled: false                        # true once DISCORD_WEBHOOK_URL is provisioned; env-gated regardless
  timeout_seconds: 5
upload:
  accept_heic: true                     # M4: HEIC now accepted (heic-convert WASM); JPEG/PNG still accepted
```

- [ ] **Step 8: Verify config still loads and the worker/eval suites stay green**

Run:

```bash
cd worker && uv run python -c "from notbulk.config import load_config; c=load_config('../config.yaml'); print(c['discord']['enabled'], c['discord']['timeout_seconds'], c['export']['retention_hours'], c['upload']['accept_heic'])"
```

Expected:

```
False 5 48 True
```

Then the full baseline suite (no pipeline logic changed, so the committed baseline holds):

```bash
cd worker && uv run pytest tests ../eval/tests
```

Expected (unchanged baseline):

```
214 passed, 4 skipped
```

- [ ] **Step 9: Commit**

```bash
git add migrations/004_m4_exports.sql config.yaml db/schema.sql
git commit -m "feat(db): M4 exports table and export job type"
```

(Bump `VERSION` only in the final M4 task per the plan's Global Constraints — not here.)

---

### Task 2: Discord notifier (`notbulk/discord.py`) + worker wiring

**Files:**
- Create: `worker/notbulk/discord.py`
- Create: `worker/tests/test_discord.py`
- Modify: `worker/notbulk/worker.py` (terminal-failure path → error notify; wire nothing else here — batch-complete notify lands in `identify.py`)
- Modify: `worker/notbulk/handlers/identify.py` (add the batch-complete info notify at the single-fire guard site)
- Modify: `worker/tests/test_handler_identify.py` (assert the batch-complete notify fires once and is a no-op under the disabled test config)

**Interfaces:**
- Consumes:
  - `jobqueue.fail(pool, job_id, error, *, dead) -> str` (unchanged) — returns terminal status `'failed'`/`'queued'`.
  - `identify.handle_identify(pool, storage, payload, cfg)` — after the guarded `_COMPLETE_BATCH_SQL` UPDATE it already computes `fired = cur.fetchone() is not None` and calls `jobqueue.notify_progress(pool, batch_id, "batch_complete")` only when `fired`. That guarded `fired` block is the batch-complete notify site.
  - `httpx` (already a worker dependency; `worker/scripts/download_refs.py` uses it).
  - config keys `cfg['discord']['enabled']` (bool) and `cfg['discord']['timeout_seconds']` (int) from Task 1.
- Produces:
  ```python
  # notbulk/discord.py
  def notify(cfg: dict, level: str, title: str, fields: dict[str, str]) -> None: ...
  ```
  No-op when `not cfg['discord']['enabled']` OR `DISCORD_WEBHOOK_URL` unset (warns once). Otherwise POSTs a Discord embed to `os.environ['DISCORD_WEBHOOK_URL']` via `httpx` (timeout `cfg['discord']['timeout_seconds']`), swallowing all exceptions. Never logs/returns the webhook URL. Both `worker.py` (error) and `identify.py` (info) call it.

- [ ] **Step 1: Write the failing tests**

Create `worker/tests/test_discord.py`:

```python
"""Unit tests for the sanitized Discord notifier.

No network: httpx is monkeypatched with a capture-only stub. The webhook URL is
supplied via the DISCORD_WEBHOOK_URL env var (monkeypatched), never printed.
"""
from __future__ import annotations

import logging

import pytest

from notbulk import discord


class _CaptureClient:
    """httpx.Client stand-in: records the single POST and returns a 204-like resp."""

    posted: list[tuple[str, dict, float | None]] = []

    def __init__(self, *, timeout=None):
        self._timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        _CaptureClient.posted.append((url, json, self._timeout))
        return type("Resp", (), {"status_code": 204, "raise_for_status": lambda self=None: None})()


class _RaisingClient:
    """httpx.Client stand-in whose post() raises (dead webhook / network down)."""

    def __init__(self, *, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        raise RuntimeError("connection refused")


ENABLED_CFG = {"discord": {"enabled": True, "timeout_seconds": 5}}
DISABLED_CFG = {"discord": {"enabled": False, "timeout_seconds": 5}}
WEBHOOK = "https://discord.com/api/webhooks/123/abcSECRETtoken"


@pytest.fixture(autouse=True)
def _reset_capture_and_warn_flag(monkeypatch):
    _CaptureClient.posted = []
    # Reset the module-level "warned once" flag so each test starts clean.
    monkeypatch.setattr(discord, "_warned_no_webhook", False, raising=False)
    yield


def test_enabled_and_env_set_posts_embed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    discord.notify(ENABLED_CFG, "error", "pipeline job failed",
                   {"type": "identify", "job_id": "j1", "error_class": "RuntimeError"})
    assert len(_CaptureClient.posted) == 1
    url, body, timeout = _CaptureClient.posted[0]
    assert url == WEBHOOK
    assert timeout == 5
    embed = body["embeds"][0]
    assert embed["title"] == "pipeline job failed"
    assert embed["color"] == discord._COLOR["error"]
    names = {f["name"]: f["value"] for f in embed["fields"]}
    assert names == {"type": "identify", "job_id": "j1", "error_class": "RuntimeError"}


def test_field_values_are_sanitized_and_truncated(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    long = "x" * 5000
    discord.notify(ENABLED_CFG, "info", "batch complete",
                   {"batch_id": "  b1  ", "note": long, "count": 7})
    embed = _CaptureClient.posted[0][1]["embeds"][0]
    vals = {f["name"]: f["value"] for f in embed["fields"]}
    assert vals["batch_id"] == "b1"          # str + strip
    assert vals["count"] == "7"              # coerced to str
    assert len(vals["note"]) == 1000         # truncated to 1000 chars


def test_disabled_config_never_posts(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    discord.notify(DISABLED_CFG, "error", "t", {"a": "b"})
    assert _CaptureClient.posted == []


def test_env_unset_never_posts_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(discord.httpx, "Client", _CaptureClient)
    with caplog.at_level(logging.WARNING):
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
    assert _CaptureClient.posted == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1                # warned ONCE, not per call


def test_post_exception_is_swallowed(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _RaisingClient)
    # Must NOT raise — a dead webhook can never crash the worker.
    discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})


def test_webhook_url_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.httpx, "Client", _RaisingClient)
    with caplog.at_level(logging.WARNING):
        discord.notify(ENABLED_CFG, "error", "t", {"a": "b"})
    assert WEBHOOK not in caplog.text
    assert "abcSECRETtoken" not in caplog.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd worker && uv run pytest tests/test_discord.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'notbulk.discord'` (collection error).

- [ ] **Step 3: Implement `notbulk/discord.py`**

Create `worker/notbulk/discord.py`:

```python
"""Sanitized Discord webhook notifier (design S10).

A single entry point `notify(cfg, level, title, fields)`:
  * No-op when the config toggle `discord.enabled` is false OR when the
    `DISCORD_WEBHOOK_URL` env var is unset (warns ONCE via a module flag so a
    disabled/unprovisioned deployment does not spam the log).
  * Otherwise POSTs a minimal Discord embed to the webhook URL via httpx, with a
    timeout of `cfg['discord']['timeout_seconds']`.
  * Every field VALUE is coerced to str, stripped, and truncated to 1000 chars —
    callers pass an error CLASS name + ids, never a raw traceback with
    interpolated user content (filenames, OCR text).
  * ALL exceptions (httpx/network/HTTP) are swallowed and logged at WARNING
    level: a dead or slow webhook must NEVER crash or stall the worker.
  * The webhook URL is NEVER logged, printed, or returned (secret hygiene).
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("notbulk.discord")

# Discord embed sidebar colors (decimal int): red for errors, green for info.
_COLOR = {"error": 0xE03131, "info": 0x2F9E44}
_DEFAULT_COLOR = 0x868E96  # gray fallback for an unknown level

_MAX_FIELD_VALUE = 1000

# Set once when DISCORD_WEBHOOK_URL is missing so the warning fires a single time
# per process rather than on every notify call.
_warned_no_webhook = False


def _sanitize(value) -> str:
    """Coerce to str, strip, truncate to _MAX_FIELD_VALUE chars."""
    return str(value).strip()[:_MAX_FIELD_VALUE]


def notify(cfg: dict, level: str, title: str, fields: dict[str, str]) -> None:
    global _warned_no_webhook

    if not cfg.get("discord", {}).get("enabled"):
        return

    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        if not _warned_no_webhook:
            log.warning("discord.enabled but DISCORD_WEBHOOK_URL unset; "
                        "notifications disabled (this warns once)")
            _warned_no_webhook = True
        return

    embed = {
        "title": title,
        "color": _COLOR.get(level, _DEFAULT_COLOR),
        "fields": [
            {"name": str(name), "value": _sanitize(value), "inline": True}
            for name, value in fields.items()
        ],
    }
    timeout = cfg.get("discord", {}).get("timeout_seconds", 5)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"embeds": [embed]})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a dead webhook must never crash the worker
        # NEVER include `url` in this message (it carries the secret token).
        log.warning("discord notify failed: %s", exc.__class__.__name__)
```

- [ ] **Step 4: Run the notifier tests to verify they pass**

Run:

```bash
cd worker && uv run pytest tests/test_discord.py -v
```

Expected: PASS — all 6 tests green.

- [ ] **Step 5: Write the failing worker-wiring test (terminal failure → error notify)**

Create `worker/tests/test_worker.py`:

```python
"""Worker-loop wiring tests: a terminal job failure emits a sanitized Discord
error notify carrying the exception CLASS name (never str(exc) / a traceback).

No network, no real DB: FakePool feeds the claim + fail SQL, notify is a spy.
"""
from __future__ import annotations

from notbulk import worker
from tests.fakes import FakePool


TEST_CFG = {"discord": {"enabled": False, "timeout_seconds": 5}}


def _boom_handler(pool, storage, payload, cfg):
    raise ValueError("secret filename /home/u/IMG_4211.heic leaked into message")


def test_terminal_failure_emits_error_notify_with_class_only(monkeypatch):
    # claim() -> one 'detect' job; fail() -> RETURNING 'failed' (terminal).
    pool = FakePool([
        [("job-1", "detect", {"photo_id": "p1"})],   # _CLAIM_SQL RETURNING id,type,payload
        [("failed",)],                                # _FAIL_DEAD_SQL RETURNING status
    ])
    spy: list[tuple] = []
    monkeypatch.setattr(worker.discord, "notify",
                        lambda cfg, level, title, fields: spy.append((level, title, fields)))

    handled = worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": _boom_handler}, worker_id="w1",
    )
    assert handled is True
    assert len(spy) == 1
    level, title, fields = spy[0]
    assert level == "error"
    assert title == "pipeline job failed"
    assert fields["type"] == "detect"
    assert fields["job_id"] == "job-1"
    assert fields["error_class"] == "ValueError"     # CLASS name only
    # The raw message (with the leaked filename) is NEVER in the notify fields.
    assert all("IMG_4211" not in str(v) for v in fields.values())


def test_success_emits_no_notify(monkeypatch):
    pool = FakePool([
        [("job-2", "detect", {"photo_id": "p1"})],   # claim
        [],                                           # complete (no RETURNING)
    ])
    spy: list = []
    monkeypatch.setattr(worker.discord, "notify",
                        lambda *a, **k: spy.append(a))
    worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": lambda *a: None}, worker_id="w1",
    )
    assert spy == []
```

- [ ] **Step 6: Run it to verify it fails**

Run:

```bash
cd worker && uv run pytest tests/test_worker.py -v
```

Expected: FAIL — `AttributeError: module 'notbulk.worker' has no attribute 'discord'` (worker.py does not import discord yet, and the fail path does not call notify).

- [ ] **Step 7: Wire the error notify into `worker.py`**

In `worker/notbulk/worker.py`, add the import beside the other `from . import` lines (below `from . import jobqueue`):

```python
from . import discord
from . import jobqueue
```

Then, in `_process_one`, replace the terminal-failure block so a `'failed'` terminal status emits a sanitized error notify. The current block is:

```python
        terminal = jobqueue.fail(pool, job_id, str(exc), dead=dead)
        if job_type == "identify" and terminal == "failed":
            _mark_card_unreadable(pool, payload.get("card_id"), str(exc))
    return True
```

Replace it with:

```python
        terminal = jobqueue.fail(pool, job_id, str(exc), dead=dead)
        if terminal == "failed":
            # Sanitized error notify (design S10): error CLASS + ids only — never
            # str(exc) / a traceback (which can carry interpolated user content).
            discord.notify(
                cfg, "error", "pipeline job failed",
                {
                    "type": job_type,
                    "job_id": job_id,
                    "batch_id": str(payload.get("batch_id") or "n/a"),
                    "error_class": exc.__class__.__name__,
                },
            )
            if job_type == "identify":
                _mark_card_unreadable(pool, payload.get("card_id"), str(exc))
    return True
```

(Note: identify payloads carry `card_id`, not `batch_id` — the `batch_id` field resolves to `'n/a'` for those; the export worker is a separate Node process, so its failures never reach this Python path.)

- [ ] **Step 8: Run the worker-wiring tests to verify they pass**

Run:

```bash
cd worker && uv run pytest tests/test_worker.py -v
```

Expected: PASS — both tests green.

- [ ] **Step 9: Write the failing batch-complete notify test (in the identify handler)**

The batch-complete Discord post is emitted at the identify handler's existing single-fire guard (the `fired` block), keeping the notify next to the code that already knows `batch_id` and already fires exactly once. Add to `worker/tests/test_handler_identify.py`:

```python
def test_batch_complete_emits_info_notify_once(monkeypatch):
    """When the guarded batch-completion UPDATE transitions the batch (fires),
    the identify handler emits ONE info Discord notify; a non-firing run emits
    none. discord.notify itself no-ops under the disabled test config."""
    from notbulk.handlers import identify as identify_handler

    spy: list[tuple] = []
    monkeypatch.setattr(identify_handler.discord, "notify",
                        lambda cfg, level, title, fields: spy.append((level, title, fields)))

    # Firing run: _COMPLETE_BATCH_SQL RETURNS a row -> fired True.
    identify_handler._notify_batch_complete(
        cfg={"discord": {"enabled": False, "timeout_seconds": 5}},
        batch_id="batch-7", fired=True,
    )
    assert len(spy) == 1
    level, title, fields = spy[0]
    assert level == "info"
    assert title == "batch complete"
    assert fields["batch_id"] == "batch-7"

    # Non-firing run (a racing worker saw 0 rows) -> no notify.
    spy.clear()
    identify_handler._notify_batch_complete(
        cfg={"discord": {"enabled": False, "timeout_seconds": 5}},
        batch_id="batch-7", fired=False,
    )
    assert spy == []
```

- [ ] **Step 10: Run it to verify it fails**

Run:

```bash
cd worker && uv run pytest tests/test_handler_identify.py::test_batch_complete_emits_info_notify_once -v
```

Expected: FAIL — `AttributeError: module 'notbulk.handlers.identify' has no attribute 'discord'` (and no `_notify_batch_complete`).

- [ ] **Step 11: Add the batch-complete notify to `identify.py`**

In `worker/notbulk/handlers/identify.py`, add the import beside the existing `from .. import jobqueue`:

```python
from .. import discord
from .. import jobqueue
```

Add this small pure helper near the bottom of the module (above `handle_identify` is fine — module order does not matter):

```python
def _notify_batch_complete(cfg: dict, batch_id: str, fired: bool) -> None:
    """Emit ONE info Discord notify when the batch actually transitioned.

    Pure additive: discord.notify no-ops when discord.enabled is false, so this
    is inert in tests and in any deployment without a provisioned webhook.
    """
    if not fired:
        return
    discord.notify(cfg, "info", "batch complete", {"batch_id": str(batch_id)})
```

Then, at the END of `handle_identify`, the current single-guarded block is:

```python
    # Single-guarded batch completion.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COMPLETE_BATCH_SQL, (batch_id, batch_id, batch_id))
            fired = cur.fetchone() is not None
        conn.commit()
    if fired:
        jobqueue.notify_progress(pool, batch_id, "batch_complete")
```

Replace it with (add the Discord post right after the existing SSE progress notify — same guard, additive):

```python
    # Single-guarded batch completion.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_COMPLETE_BATCH_SQL, (batch_id, batch_id, batch_id))
            fired = cur.fetchone() is not None
        conn.commit()
    if fired:
        jobqueue.notify_progress(pool, batch_id, "batch_complete")
    _notify_batch_complete(cfg, batch_id, fired)
```

- [ ] **Step 12: Run the batch-complete test to verify it passes**

Run:

```bash
cd worker && uv run pytest tests/test_handler_identify.py::test_batch_complete_emits_info_notify_once -v
```

Expected: PASS.

- [ ] **Step 13: Run the full worker + eval suite (no regressions; new tests added)**

Run:

```bash
cd worker && uv run pytest tests ../eval/tests
```

Expected: the baseline plus the new discord + worker tests, all green — `223 passed, 4 skipped` (214 baseline + 6 in `test_discord.py` + 2 in `test_worker.py` + 1 in `test_handler_identify.py`). Confirm zero failures; the exact passed count may differ if other Task 2 iterations added/removed a case, but there must be **0 failed** and the skip count stays 4.

- [ ] **Step 14: Commit**

```bash
git add worker/notbulk/discord.py worker/tests/test_discord.py \
        worker/notbulk/worker.py worker/tests/test_worker.py \
        worker/notbulk/handlers/identify.py worker/tests/test_handler_identify.py
git commit -m "feat(worker): sanitized Discord notifications on errors and batch completion"
```

---

### Task 3: Type-partitioned job claim

**Files:**
- Modify: `worker/notbulk/jobqueue.py` (`_CLAIM_SQL` inner SELECT + `claim` signature)
- Modify: `worker/notbulk/worker.py` (pass `allowed_types` derived from `_build_handlers` keys; assert coverage)
- Modify: `worker/tests/test_jobqueue.py` (type-filter unit + integration cases)
- Create: `worker/tests/test_worker_claim.py` (assert `worker.py`'s allowed_types == `_build_handlers` keys)

**Interfaces:**
- Consumes:
  - current `_CLAIM_SQL` (see `jobqueue.py`) — the inner `SELECT id FROM jobs WHERE status='queued' AND run_after<=now() ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED`.
  - `worker._build_handlers() -> dict[str, handler]` with keys `{"detect","identify","fetch_source","ingest_correction","price"}`.
  - `jobqueue.claim(pool, worker_id)` call site in `worker._process_one`.
- Produces:
  ```python
  # notbulk/jobqueue.py  (MODIFY)
  def claim(pool, worker_id: str, allowed_types: tuple[str, ...]) -> tuple[str, str, dict] | None: ...
  ```
  The inner SELECT gains `AND type = ANY(%s)` bound to `list(allowed_types)` (parameterized). `worker._process_one` passes `allowed_types` matching its handler keys. Guarantees the Python worker never claims an `'export'` job (no handler → would dead-letter).

- [ ] **Step 1: Write the failing unit test (type filter is bound, not interpolated)**

Add to `worker/tests/test_jobqueue.py` (near the claim/reclaim block):

```python
def test_claim_binds_allowed_types_into_sql():
    """The inner SELECT gains `AND type = ANY(%s)` bound to list(allowed_types)
    — parameterized, never interpolated. FakePool returns a detect row."""
    pool = FakePool([[("job-1", "detect", {"photo_id": "p1"})]])
    claimed = jobqueue.claim(pool, "w1", allowed_types=("detect", "identify"))
    assert claimed == ("job-1", "detect", {"photo_id": "p1"})
    sql, params = pool.cursor.executed[0]
    nospace = sql.lower().replace(" ", "")
    assert "type=any(%s)" in nospace                 # the type filter is present
    # params: (worker_id, [allowed types]) — the list is a bound parameter.
    assert params[0] == "w1"
    assert params[1] == ["detect", "identify"]       # list(), bound not interpolated


def test_claim_returns_none_when_no_matching_type():
    """No queued row of an allowed type -> FakePool yields [] -> None."""
    pool = FakePool([[]])
    assert jobqueue.claim(pool, "w1", allowed_types=("detect",)) is None
```

- [ ] **Step 2: Run to verify they fail**

Run:

```bash
cd worker && uv run pytest tests/test_jobqueue.py::test_claim_binds_allowed_types_into_sql tests/test_jobqueue.py::test_claim_returns_none_when_no_matching_type -v
```

Expected: FAIL — `TypeError: claim() missing 1 required positional argument: 'allowed_types'` (and the current SQL has no `type = ANY`).

- [ ] **Step 3: Modify `_CLAIM_SQL` and `claim` in `jobqueue.py`**

In `worker/notbulk/jobqueue.py`, update `_CLAIM_SQL` — add `AND type = ANY(%s)` to the inner SELECT (parameter order: `%s` for `locked_by`/worker_id in the outer UPDATE stays first, then the type list):

```python
# Exact claim SQL from the Interface Contract, extended with a type partition:
# the inner SELECT gains `AND type = ANY(%s)` so N workers of different classes
# (Python pipeline vs Node export) never claim each other's jobs.
_CLAIM_SQL = (
    "UPDATE jobs SET status='running', locked_at=now(), locked_by=%s, "
    "attempts=attempts+1, updated_at=now() "
    "WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now() "
    "AND type = ANY(%s) "
    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, type, payload"
)
```

And update `claim`:

```python
def claim(pool, worker_id: str, allowed_types: tuple[str, ...]) -> tuple[str, str, dict] | None:
    """Atomically claim the oldest runnable job whose type is in allowed_types.
    Returns (id, type, payload) or None. payload is already a dict (jsonb decodes
    to dict via psycopg).

    allowed_types partitions the shared jobs table: the Python pipeline worker
    passes its handler types; the Node export worker claims only 'export'. A
    worker must never claim a type it has no handler for (it would dead-letter).
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CLAIM_SQL, (worker_id, list(allowed_types)))
            row = cur.fetchone()
        conn.commit()
    if row is None:
        return None
    job_id, job_type, payload = row
    # psycopg returns jsonb as a Python object already; be defensive if a
    # driver hands back a str.
    if isinstance(payload, str):
        payload = json.loads(payload)
    return str(job_id), job_type, payload
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run:

```bash
cd worker && uv run pytest tests/test_jobqueue.py::test_claim_binds_allowed_types_into_sql tests/test_jobqueue.py::test_claim_returns_none_when_no_matching_type -v
```

Expected: PASS.

- [ ] **Step 5: Update the `claim` call in `worker.py` and assert handler coverage**

In `worker/notbulk/worker.py`, derive `allowed_types` from the handler dict so the partition can never drift from the registered handlers. Change `_process_one` to take `allowed_types` and pass it through:

```python
def _process_one(pool, storage, cfg, handlers, worker_id: str, allowed_types: tuple[str, ...]) -> bool:
    """Claim and run a single job. Returns True if a job was processed."""
    claimed = jobqueue.claim(pool, worker_id, allowed_types)
    if claimed is None:
        return False
    job_id, job_type, payload = claimed
    handler = handlers.get(job_type)
    try:
        if handler is None:
            raise ValueError(f"no handler for job type {job_type!r}")
        jobqueue.validate_payload(job_type, payload)
        handler(pool, storage, payload, cfg)
        jobqueue.complete(pool, job_id)
    except Exception as exc:  # noqa: BLE001 — worker must never crash on a bad job
        # Full trace to stderr; only str(exc) is persisted (sanitized).
        traceback.print_exc()
        dead = _is_permanent(exc)
        terminal = jobqueue.fail(pool, job_id, str(exc), dead=dead)
        if terminal == "failed":
            discord.notify(
                cfg, "error", "pipeline job failed",
                {
                    "type": job_type,
                    "job_id": job_id,
                    "batch_id": str(payload.get("batch_id") or "n/a"),
                    "error_class": exc.__class__.__name__,
                },
            )
            if job_type == "identify":
                _mark_card_unreadable(pool, payload.get("card_id"), str(exc))
    return True
```

(This folds in the Task 2 error-notify block; if Task 2's edit is already present, only the signature and the `jobqueue.claim(pool, worker_id, allowed_types)` call change.)

In `main()`, after `handlers = _build_handlers()`, derive and assert the partition, then thread it into the drain loop. Replace:

```python
    handlers = _build_handlers()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
```

with:

```python
    handlers = _build_handlers()
    # The Python pipeline worker claims ONLY its handler types (queue partition
    # invariant): the Node export worker owns 'export'. Deriving allowed_types
    # from the handler keys makes drift impossible — every claimed type has a
    # handler, so a claimed job can never dead-letter for lack of one.
    allowed_types = tuple(sorted(handlers))
    assert all(t in handlers for t in allowed_types), "allowed_types must all have handlers"
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
```

And update the drain loop call inside `main()`:

```python
            # Drain everything claimable right now.
            while not stopper.stop and _process_one(pool, storage, cfg, handlers, worker_id, allowed_types):
                pass
```

- [ ] **Step 6: Update the existing worker-wiring tests for the new `_process_one` signature**

The Task 2 tests in `worker/tests/test_worker.py` call `_process_one(...)` without `allowed_types`. Update both calls to pass it (they must match the handler dict they supply):

```python
    handled = worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": _boom_handler}, worker_id="w1",
        allowed_types=("detect",),
    )
```

and

```python
    worker._process_one(
        pool, storage=None, cfg=TEST_CFG,
        handlers={"detect": lambda *a: None}, worker_id="w1",
        allowed_types=("detect",),
    )
```

- [ ] **Step 7: Write the worker allowed_types-coverage unit test**

Create `worker/tests/test_worker_claim.py`:

```python
"""The Python worker must claim exactly its handler types — no more (would claim
an unhandled 'export' job and dead-letter it), no fewer (a handler would starve).
This asserts the partition the worker uses stays in lockstep with _build_handlers.
"""
from __future__ import annotations

from notbulk import worker


def test_worker_allowed_types_equal_handler_keys():
    handlers = worker._build_handlers()
    allowed = set(tuple(sorted(handlers)))          # mirrors main()'s derivation
    assert allowed == set(handlers)
    # Explicit: the five pipeline types, and NOT 'export' (Node-owned).
    assert allowed == {"detect", "identify", "fetch_source", "ingest_correction", "price"}
    assert "export" not in allowed
```

- [ ] **Step 8: Write the integration test (real DB, type-filtered claim partitions cleanly)**

Add to `worker/tests/test_jobqueue.py` (below the existing `test_claim_reclaim_backoff_against_real_db`, reusing the same skip guard + inline-DSN + self-cleaning pattern):

```python
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set (export the compose-local DSN to run this)",
)
def test_type_partitioned_claim_against_real_db():
    """A mix of queued types: a detect-only worker claims only detect jobs and
    SKIPs a queued price job; two allowed-type sets partition the queue cleanly
    (no job claimed twice, no job of the wrong class claimed). Self-cleaning."""
    import os as _os

    from psycopg_pool import ConnectionPool

    from notbulk import jobqueue as jq

    dsn = _os.environ["DATABASE_URL"]
    pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=4, open=True)
    tag = f"itest-{_uuid.uuid4().hex[:8]}"

    def _insert(job_type, payload):
        job_id = str(_uuid.uuid4())
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs (id, type, payload, status) VALUES (%s, %s, %s, 'queued')",
                    (job_id, job_type, json.dumps(payload)),
                )
            conn.commit()
        return job_id

    try:
        d1 = _insert("detect", {"photo_id": tag})
        p1 = _insert("price", {"card_ref_id": tag, "finish": "normal"})
        d2 = _insert("detect", {"photo_id": tag})

        # A detect-only worker claims both detect jobs and never the price job.
        claimed_detect = []
        while True:
            c = jq.claim(pool, "w-detect", allowed_types=("detect",))
            if c is None:
                break
            if c[2].get("photo_id") == tag or c[2].get("card_ref_id") == tag:
                claimed_detect.append((c[0], c[1]))
        claimed_ids = {jid for jid, _ in claimed_detect}
        assert claimed_ids == {d1, d2}
        assert all(t == "detect" for _, t in claimed_detect)
        assert p1 not in claimed_ids                 # price job SKIPPED by type filter

        # The price job is still queued and claimable by a price-class worker.
        c_price = jq.claim(pool, "w-price", allowed_types=("price",))
        assert c_price is not None and c_price[0] == p1 and c_price[1] == "price"
    finally:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs WHERE payload->>'photo_id' = %s "
                    "OR payload->>'card_ref_id' = %s",
                    (tag, tag),
                )
            conn.commit()
        pool.close()
```

Also update the EXISTING `test_claim_reclaim_backoff_against_real_db` claim calls to pass the new argument (three call sites: `jq.claim(pool, "w1")`, `jq.claim(pool, "w2")`):

```python
        first = jq.claim(pool, "w1", allowed_types=("detect",))
```

```python
                skipped = jq.claim(pool, "w2", allowed_types=("detect",))
```

- [ ] **Step 9: Run the jobqueue + worker-claim tests (unit; integration skips without DATABASE_URL)**

Run:

```bash
cd worker && uv run pytest tests/test_jobqueue.py tests/test_worker_claim.py tests/test_worker.py -v
```

Expected: PASS — new unit tests green; both `*_against_real_db` tests print `SKIPPED (DATABASE_URL not set ...)` (they run only when the DSN is exported).

- [ ] **Step 10: Run the integration test against the real DB (optional but recommended)**

Run (compose Postgres up; inline DSN enables the skip-guarded tests):

```bash
cd worker && DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  uv run pytest tests/test_jobqueue.py -k "against_real_db" -v
```

Expected: both real-DB tests PASS (`test_claim_reclaim_backoff_against_real_db`, `test_type_partitioned_claim_against_real_db`).

- [ ] **Step 11: Run the full worker + eval suite (no regressions)**

Run:

```bash
cd worker && uv run pytest tests ../eval/tests
```

Expected: all green, **0 failed**. Skip count stays 4 unless the DSN is exported (then the two real-DB jobqueue tests run instead of skipping). New tests from this task: 2 in `test_jobqueue.py` (unit) + 1 real-DB (skipped by default) + 1 in `test_worker_claim.py`.

- [ ] **Step 12: Commit**

```bash
git add worker/notbulk/jobqueue.py worker/notbulk/worker.py \
        worker/tests/test_jobqueue.py worker/tests/test_worker_claim.py \
        worker/tests/test_worker.py
git commit -m "feat(worker): type-partitioned job claim for multi-worker queue"
```

---

## Section self-review (Tasks 1–3)

- **Spec coverage:** migration 004 + config (Task 1) ← contract "Migration 004" + "config.yaml additions"; Discord notifier + error/batch-complete wiring (Task 2) ← "Python signatures: notify", "Worker Discord wiring", Global Constraint "Discord messages are SANITIZED"; type-partitioned claim (Task 3) ← "claim SQL adds `AND type = ANY(%s)`", Global Constraint "Queue partition invariant".
- **Type consistency:** `notify(cfg, level, title, fields)` and `claim(pool, worker_id, allowed_types)` match the contract signatures exactly; `_process_one` gains `allowed_types` and every call site (main loop + both Task 2 tests) is updated in-section; `allowed_types` is derived from `_build_handlers()` keys with an assertion, so it cannot drift.
- **No placeholders:** every code step shows complete code; every run step gives the exact command + expected output; the eval baseline (214 passed / 4 skipped) is stated and re-checked after each task.
<!-- Assembled into 2026-07-17-m4-pdf-export.md. Conforms to that plan's Global Constraints,
     Interface Contract, and File Structure. Node 20 web tests run with:
       export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run
-->

### Task 4: HEIC upload support (imagegate)

**Files:**
- Modify: `web/src/services/imagegate.ts` (add HEIF magic-byte sniff; format-tag return; `accept_heic` gate)
- Modify: `web/src/config.ts:6-39` (add `upload: { accept_heic: boolean }` to the `Config` interface)
- Modify: `web/tests/imagegate.test.ts` (extend the existing `cfg` stub; add the HEIC describe block)
- Add dependency: `heic-convert` + `@types/heic-convert` (Step 4a).
- Use (do not edit — already committed): `web/tests/fixtures/sample-card.heic` — a real 1053-byte `heic`-branded fixture.
- Reference (do not edit): `config.yaml` `upload.accept_heic: true` block already specified in the plan's "config.yaml additions" — Task 1 writes it; Task 4 only consumes it.

**Interfaces:**
- Consumes: `Config` from `../config.js` — after this task it carries `upload: { accept_heic: boolean }`. The existing `quotas.max_photo_bytes` / `quotas.max_pixels` are unchanged.
- Produces:
  - `isSupportedImage(bytes: Buffer): 'jpeg' | 'png' | 'heif' | null` — a **format-tag sniff** (renamed contract from the old boolean `hasMagic`). Returns the detected format or `null`. No config dependency: pure byte inspection.
  - `gateImage(bytes: Buffer, cfg: Config): Promise<GateResult | GateReject>` — unchanged signature. Now: sniffs via `isSupportedImage`; rejects `null` with `'unsupported format'`; rejects `'heif'` with `'unsupported format'` when `!cfg.upload.accept_heic` (the config kill-switch); when the tag is `'heif'` and accepted, decodes with **`heic-convert`** (`heicConvert({buffer, format:'JPEG', quality:0.92})`) → a JPEG buffer → the **existing** `sharp(jpeg,...).rotate().webp()` pipeline (EXIF stripped, nothing raw stored). JPEG/PNG feed straight into sharp (no heic-convert). The no-throw `GateReject` contract (AC-8) is preserved by the existing `try/catch` — a heic-convert or sharp failure returns `'corrupt image'`, never throws.

**Design note (verified on this box — read before writing the test — Assembly Resolution 1):**
sharp reports `format.heif.input: true` but **cannot actually decode HEVC-compressed HEIC** here — its bundled libheif lacks the HEVC decoder plugin, and a real HEIC upload fails with `Support for this compression format has not been built in`. So HEIC decode uses the pure-JS **`heic-convert`** package (bundles a WASM HEVC decoder, no system libs) to turn HEIC → JPEG, then the existing sharp pipeline re-encodes to WebP. A **real committed fixture** at `web/tests/fixtures/sample-card.heic` (1053 bytes, `heic`-branded, structured content) is verified decodable by heic-convert → 8548-byte JPEG → sharp WebP. Tests assert **genuine decode-success** on that real fixture (not a hand-crafted header): `accept_heic:true` → `{ ok:true }` with a real WebP out. Also assert `accept_heic:false` → sniff-reject `'unsupported format'` (heic-convert never called); a truncated HEIC (`fixture.subarray(0,40)` — passes the ftyp sniff, fails decode) → `'corrupt image'` no-throw; JPEG/PNG unchanged; GIF still rejected. heic-convert is a new dependency in the highest-risk decode path (S7) — pin its version.

- [ ] **Step 1: Extend the `Config` interface with the `upload` block**

In `web/src/config.ts`, add the `upload` field to the `Config` interface (insert after the `explorer` line, keeping the interface a single source of truth):

```ts
export interface Config {
  web: { port: number; base_url: string; secure_cookies: boolean };
  storage: {
    endpoint: string;
    bucket: string;
    access_key: string;
    secret_key: string;
    signed_url_ttl_seconds: number;
  };
  mail: { smtp_host: string; smtp_port: number; from: string };
  auth: {
    session_absolute_days: number;
    session_idle_days: number;
    magic_link_expiry_minutes: number;
    magic_links_per_email_hour: number;
    magic_links_per_email_day: number;
  };
  quotas: {
    batches_per_day: number;
    photos_per_day: number;
    cards_per_day: number;
    fetches_per_day: number;
    photos_per_batch: number;
    anon_photos_per_batch: number;
    max_photo_bytes: number;
    max_pixels: number;
    max_cards_per_photo: number;
  };
  fetcher: { allowed_hosts: string[]; max_bytes: number; timeout_seconds: number };
  hash: { user_validated_cap_per_card: number };
  turnstile: { site_key: string; secret: string };
  refproxy: { allowed_image_host: string; cache_prefix: string; max_bytes: number };
  explorer: { page_size: number; default_sort: string };
  upload: { accept_heic: boolean };
}
```

- [ ] **Step 2: Write the failing tests**

Append to `web/tests/imagegate.test.ts`. First extend the file's `cfg` stub so it carries `upload.accept_heic` (default `true`), and add a small `cfgNoHeic` variant. Load the **real** committed HEIC fixture (`web/tests/fixtures/sample-card.heic`) with `fs` — the HEIC assertions call the real `heic-convert` on it for a genuine decode-success proof (it's fast on a 1 KB fixture; no mock).

```ts
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_HEIC = readFileSync(join(HERE, 'fixtures/sample-card.heic')); // real heic-branded, 1053 bytes

// --- extend the top-of-file cfg stub: add upload.accept_heic ---
// Replace the existing `const cfg = {...} as unknown as Config;` with:
const cfg = {
  quotas: { max_pixels: 50_000_000, max_photo_bytes: 10_485_760 },
  upload: { accept_heic: true },
} as unknown as Config;

const cfgNoHeic = {
  quotas: { max_pixels: 50_000_000, max_photo_bytes: 10_485_760 },
  upload: { accept_heic: false },
} as unknown as Config;

describe('gateImage — HEIC (M4)', () => {
  it("isSupportedImage tags the real HEIC (and other HEIF brands) as 'heif'", async () => {
    const { isSupportedImage } = await import('../src/services/imagegate.js');
    expect(isSupportedImage(REAL_HEIC)).toBe('heif');
    // brand-set coverage via a minimal ftyp header (sniff-only, not decoded here):
    const ftyp = (brand: string) => {
      const b = Buffer.alloc(12);
      b.writeUInt32BE(12, 0); b.write('ftyp', 4, 'latin1'); b.write(brand, 8, 'latin1');
      return b;
    };
    expect(isSupportedImage(ftyp('mif1'))).toBe('heif');
    expect(isSupportedImage(ftyp('hevc'))).toBe('heif');
  });

  it("isSupportedImage still tags JPEG and PNG, and returns null for GIF", async () => {
    const { isSupportedImage } = await import('../src/services/imagegate.js');
    expect(isSupportedImage(await solid(32, 32, 'jpeg'))).toBe('jpeg');
    expect(isSupportedImage(await solid(32, 32, 'png'))).toBe('png');
    expect(isSupportedImage(Buffer.from('GIF89a', 'latin1'))).toBeNull();
  });

  it('accept_heic=true: the REAL HEIC decodes end-to-end to a WebP (genuine decode-success)', async () => {
    const res = await gateImage(REAL_HEIC, cfg);
    expect(res.ok).toBe(true);
    if (res.ok) {
      // WebP magic: bytes 0..4 'RIFF', bytes 8..12 'WEBP'
      expect(res.webp.subarray(0, 4).toString('latin1')).toBe('RIFF');
      expect(res.webp.subarray(8, 12).toString('latin1')).toBe('WEBP');
      expect(res.width).toBeGreaterThan(0);
      expect(res.height).toBeGreaterThan(0);
    }
  });

  it("accept_heic=false: the SAME real HEIC is sniff-rejected 'unsupported format' before any decode (kill-switch)", async () => {
    const res = await gateImage(REAL_HEIC, cfgNoHeic);
    expect(res).toEqual({ ok: false, reason: 'unsupported format' });
  });

  it("truncated HEIC (passes ftyp sniff, fails decode) → 'corrupt image', never throws (AC-8)", async () => {
    const truncated = REAL_HEIC.subarray(0, 40); // still has the ftyp header, body cut off
    await expect(gateImage(truncated, cfg)).resolves.toEqual({ ok: false, reason: 'corrupt image' });
  });

  it('does NOT regress JPEG/PNG accept (heic-convert not invoked for them)', async () => {
    expect((await gateImage(await solid(200, 120, 'jpeg'), cfg)).ok).toBe(true);
    expect((await gateImage(await solid(64, 64, 'png'), cfg)).ok).toBe(true);
  });

  it("still rejects a GIF with 'unsupported format'", async () => {
    const res = await gateImage(Buffer.from('GIF89a', 'latin1'), cfg);
    expect(res).toEqual({ ok: false, reason: 'unsupported format' });
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/imagegate.test.ts`
Expected: FAIL — `isSupportedImage` is not exported yet (import error / `isSupportedImage is not a function`), and the real-HEIC cases fail because the current gate rejects the `ftyp` bytes as `'unsupported format'` unconditionally (no HEIF branch, no heic-convert). The pre-existing JPEG/PNG/GIF tests still pass.

- [ ] **Step 4a: Add the heic-convert dependency**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm add heic-convert@2.1.0 && pnpm add -D @types/heic-convert@2.1.0`
(Pin exact versions — heic-convert bundles a WASM HEVC decoder and is a new dependency in the highest-risk decode path, S7. Verify it resolves; `pnpm why heic-convert` should show it.)

- [ ] **Step 4: Implement — rewrite imagegate.ts with the HEIF sniff + heic-convert decode**

Replace the whole of `web/src/services/imagegate.ts` with:

```ts
import sharp from 'sharp';
// heic-convert has no ESM default-export types in some versions; import as CJS interop.
import heicConvert from 'heic-convert';
import type { Config } from '../config.js';

export interface GateResult {
  ok: true;
  webp: Buffer;
  width: number;
  height: number;
}
export interface GateReject {
  ok: false;
  reason: string;
}

export type ImageFormat = 'jpeg' | 'png' | 'heif';

const JPEG_MAGIC = Buffer.from([0xff, 0xd8, 0xff]);
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

// ISO-BMFF HEIF brands (bytes[8..12] of an `ftyp` box). heic-convert decodes
// all of these. `avif` is deliberately NOT here — AVIF is a different codec path
// and out of scope for M4's HEIC support.
const HEIF_BRANDS = new Set([
  'heic', 'heix', 'hevc', 'hevx', 'mif1', 'msf1', 'heim', 'heis', 'hevm', 'hevs',
]);

/**
 * Sniff the container format from magic bytes only — no decode, no config.
 * Returns the format tag or null. HEIF detection: an ISO-BMFF `ftyp` box
 * (bytes[4..8] === 'ftyp') whose major brand (bytes[8..12]) is a known HEIF brand.
 */
export function isSupportedImage(bytes: Buffer): ImageFormat | null {
  if (bytes.length >= 3 && bytes.subarray(0, 3).equals(JPEG_MAGIC)) return 'jpeg';
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(PNG_MAGIC)) return 'png';
  if (
    bytes.length >= 12 &&
    bytes.subarray(4, 8).toString('latin1') === 'ftyp' &&
    HEIF_BRANDS.has(bytes.subarray(8, 12).toString('latin1'))
  ) {
    return 'heif';
  }
  return null;
}

export async function gateImage(
  bytes: Buffer,
  cfg: Config,
): Promise<GateResult | GateReject> {
  // 1. Byte-length cap FIRST — cheapest, before any decode work.
  if (bytes.length > cfg.quotas.max_photo_bytes) {
    return { ok: false, reason: 'file too large' };
  }

  // 2. Magic-byte sniff → format tag. Unknown format is rejected before sharp
  //    touches the buffer. HEIF is accepted only when the config kill-switch is on;
  //    when off, HEIF is treated like any other unsupported format (never decoded).
  const format = isSupportedImage(bytes);
  if (format === null) {
    return { ok: false, reason: 'unsupported format' };
  }
  if (format === 'heif' && !cfg.upload.accept_heic) {
    return { ok: false, reason: 'unsupported format' };
  }

  // 3. Decode + re-encode. sharp decodes JPEG/PNG natively; HEIC (HEVC) is decoded
  //    FIRST by heic-convert (sharp's bundled libheif has no HEVC decoder) into a JPEG
  //    buffer, which then feeds the SAME sharp pipeline. Either decoder throwing (a
  //    truncated/corrupt input) is caught below → GateReject. limitInputPixels enforces
  //    the pixel cap; .rotate() applies EXIF orientation; the WebP re-encode strips all
  //    metadata (nothing raw is stored). AC-8: never throws out of gateImage.
  try {
    let sharpInput = bytes;
    if (format === 'heif') {
      // WASM HEVC decode → JPEG. Throws on a malformed HEIC (caught below).
      const jpeg = await heicConvert({ buffer: bytes, format: 'JPEG', quality: 0.92 });
      sharpInput = Buffer.from(jpeg);
    }
    const pipeline = sharp(sharpInput, {
      limitInputPixels: cfg.quotas.max_pixels,
      failOn: 'error',
    })
      .rotate()
      .webp({ quality: 75 });

    const { data, info } = await pipeline.toBuffer({ resolveWithObject: true });
    return { ok: true, webp: data, width: info.width, height: info.height };
  } catch (err) {
    // sharp's pixel-limit error message contains "pixels"; distinguish it from a
    // generic decode failure so callers/tests get a precise reason.
    const msg = err instanceof Error ? err.message.toLowerCase() : '';
    if (msg.includes('pixel')) {
      return { ok: false, reason: 'image too large' };
    }
    return { ok: false, reason: 'corrupt image' };
  }
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/imagegate.test.ts`
Expected: PASS — all pre-existing JPEG/PNG/GIF/pixel-cap/AC-8 tests plus the new HEIC block. In particular the `accept_heic=true` REAL HEIC returns `{ ok:true }` with a genuine RIFF/WEBP buffer (heic-convert+sharp actually decoded it), `accept_heic=false` returns `{ ok:false, reason:'unsupported format' }` (sniff-gated, heic-convert never called), and the truncated HEIC returns `{ ok:false, reason:'corrupt image' }` (no throw).

- [ ] **Step 6: Run the full web suite to confirm no regression**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run`
Expected: PASS (any callers of `isSupportedImage`/`gateImage` — upload routes — still compile and pass; the gate's public `gateImage` signature is unchanged).

- [ ] **Step 7: Commit**

```bash
git add web/src/services/imagegate.ts web/src/config.ts web/tests/imagegate.test.ts web/tests/fixtures/sample-card.heic web/package.json web/pnpm-lock.yaml
git commit -m "feat(web): HEIC upload support via heic-convert"
```

---

### Task 5: PDF template (collection-pdf.njk)

**Files:**
- Create: `web/views/collection-pdf.njk` (standalone print-styled HTML — a full `<html>` document; NOT extended from `layout.njk`)
- Create: `web/tests/pdf.template.test.ts`

**Interfaces:**
- Consumes: the render context shape from the plan's Interface Contract (`src/lib/pdf.ts`), rendered by Task 6:
  - `cards: PdfCard[]` — `{ cropDataUri: string | null; name: string; set: string; number: string; finish: string; priceDisplay: string; quantity: number }`
  - `stats: PdfStats` — `{ totalCards: number; totalValueDisplay: string; generatedAt: string }`
  - The template references these as top-level `cards` and `stats`.
- Produces: `collection-pdf.njk` — a self-contained document (inline `<style>`, no `<script>`, no external resources; all images are `data:` URIs). Task 6 renders it with Nunjucks (autoescape on) and feeds the HTML to Puppeteer. All user-derived fields (`card.name/set/number/finish`) are autoescaped — **never** use `|safe` on them.

**Design note:** This document is rendered by headless Chrome with JavaScript disabled (Task 6 calls `setJavaScriptEnabled(false)`). There is no app CSP on a Puppeteer-rendered document, so an inline `<style>` block is fine and necessary — but we STILL keep the document script-free and resource-free (all CSS inline, all images data URIs) as defense-in-depth. Autoescaping is the XSS control: `card.name` etc. come from OCR/LLM/user data and MUST render escaped. The disclaimer is copied VERBATIM from `web/views/landing.njk:40` including the `é` in `Pokémon`.

- [ ] **Step 1: Write the failing tests**

Create `web/tests/pdf.template.test.ts`. The test renders the njk with a **standalone** Nunjucks `Environment` (autoescape on, pointed at the real `views/` dir) — NOT the Express app — so it isolates template behavior.

```ts
import { describe, it, expect } from 'vitest';
import nunjucks from 'nunjucks';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'views');

function renderPdf(ctx: Record<string, unknown>): string {
  // Standalone env — mirrors app.ts nunjucks.configure autoescape:true, but NOT the app.
  const env = nunjucks.configure(viewsDir, { autoescape: true, noCache: true });
  return env.render('collection-pdf.njk', ctx);
}

const DISCLAIMER =
  'NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by ' +
  'Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.';

const baseStats = {
  totalCards: 3,
  totalValueDisplay: '$142.50',
  generatedAt: '2026-07-17 14:03 UTC',
};

const goodCard = {
  cropDataUri: 'data:image/webp;base64,UklGRhABBBBB',
  name: 'Charizard',
  set: 'Base Set',
  number: '4/102',
  finish: 'holofoil',
  priceDisplay: '$120.00',
  quantity: 1,
};

const nullCropCard = {
  cropDataUri: null,
  name: 'Pikachu',
  set: 'Jungle',
  number: '60/64',
  finish: 'normal',
  priceDisplay: '$2.50',
  quantity: 2,
};

describe('collection-pdf.njk', () => {
  it('includes the non-affiliation disclaimer VERBATIM (é preserved)', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain(DISCLAIMER);
    expect(html).toContain('Pokémon'); // é, not Pokemon
  });

  it('renders the cover stats: total cards, total value, generated date', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('3');            // totalCards
    expect(html).toContain('$142.50');      // totalValueDisplay
    expect(html).toContain('2026-07-17 14:03 UTC'); // generatedAt
    expect(html).toContain('NotBulk');      // wordmark
    expect(html).toContain('Collection Export');
  });

  it('emits a data-URI <img> for a card with a cropDataUri', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('src="data:image/webp;base64,UklGRhABBBBB"');
  });

  it('emits a placeholder (no <img src=data:>) for a null cropDataUri', () => {
    const html = renderPdf({ cards: [nullCropCard], stats: baseStats });
    // Placeholder box present; NO image element for this card.
    expect(html).toContain('pdf-card__placeholder');
    expect(html).not.toContain('src="data:'); // this single-card render has no data-URI img
  });

  it('renders per-card name/set/number/finish/price/quantity', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('Charizard');
    expect(html).toContain('Base Set');
    expect(html).toContain('4/102');
    expect(html).toContain('holofoil');
    expect(html).toContain('$120.00');
  });

  it('ESCAPES a card name containing <script> (no XSS into the PDF)', () => {
    const evil = { ...goodCard, name: '<script>alert(1)</script>' };
    const html = renderPdf({ cards: [evil], stats: baseStats });
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).not.toContain('<script>alert(1)</script>');
  });

  it('contains NO <script> tag anywhere and no external resource references', () => {
    const html = renderPdf({ cards: [goodCard, nullCropCard], stats: baseStats });
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toMatch(/https?:\/\//i); // no external hrefs/srcs
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/pdf.template.test.ts`
Expected: FAIL — `collection-pdf.njk` does not exist (`Template render error` / `template not found`).

- [ ] **Step 3: Implement — create the template**

Create `web/views/collection-pdf.njk`. Full standalone HTML. Inline `<style>` with `@page` margins and a print-friendly grid; `page-break-inside: avoid` on each card; a footer via CSS `position: fixed` (headless Chrome paints a fixed footer on each printed page) carrying the generation date, total value, and the verbatim disclaimer. The disclaimer text is copied exactly from `web/views/landing.njk:40`.

```njk
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>NotBulk — Collection Export</title>
  <style>
    @page {
      size: Letter;
      margin: 14mm 12mm 22mm 12mm;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      padding: 0;
      font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      color: #16181d;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }

    /* Fixed footer painted on every printed page (Chrome print). */
    .pdf-footer {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      font-size: 8px;
      line-height: 1.35;
      color: #565b66;
      border-top: 1px solid #d7dae0;
      padding: 4px 0 0 0;
    }
    .pdf-footer__meta { display: flex; justify-content: space-between; margin-bottom: 2px; }
    .pdf-footer__disclaimer { max-width: 100%; }

    /* Cover section. */
    .pdf-cover {
      text-align: center;
      padding: 40mm 0 18mm 0;
      page-break-after: always;
    }
    .pdf-cover__wordmark { font-size: 40px; font-weight: 800; letter-spacing: -0.5px; }
    .pdf-cover__title { font-size: 20px; font-weight: 600; color: #444a55; margin-top: 6px; }
    .pdf-cover__stats { margin-top: 26px; font-size: 13px; line-height: 1.9; color: #23272f; }
    .pdf-cover__stats strong { font-weight: 700; }

    /* Card grid. */
    .pdf-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }
    .pdf-card {
      page-break-inside: avoid;
      break-inside: avoid;
      border: 1px solid #d7dae0;
      border-radius: 6px;
      padding: 6px;
      font-size: 9px;
      line-height: 1.35;
    }
    .pdf-card__img,
    .pdf-card__placeholder {
      width: 100%;
      height: 120px;
      object-fit: contain;
      border-radius: 4px;
      background: #f1f3f6;
      display: block;
    }
    .pdf-card__placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
      color: #8a909b;
      font-size: 8px;
    }
    .pdf-card__name { font-weight: 700; margin-top: 5px; font-size: 10px; }
    .pdf-card__meta { color: #565b66; }
    .pdf-card__price { font-weight: 700; margin-top: 3px; }
  </style>
</head>
<body>
  <!-- Footer element is fixed → Chrome repeats it on each page. -->
  <div class="pdf-footer">
    <div class="pdf-footer__meta">
      <span>Generated {{ stats.generatedAt }}</span>
      <span>Total value {{ stats.totalValueDisplay }}</span>
    </div>
    <div class="pdf-footer__disclaimer">NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.</div>
  </div>

  <section class="pdf-cover">
    <div class="pdf-cover__wordmark">NotBulk</div>
    <div class="pdf-cover__title">Collection Export</div>
    <div class="pdf-cover__stats">
      <div>Total cards: <strong>{{ stats.totalCards }}</strong></div>
      <div>Total value: <strong>{{ stats.totalValueDisplay }}</strong></div>
      <div>Generated: <strong>{{ stats.generatedAt }}</strong></div>
    </div>
  </section>

  <main class="pdf-grid">
    {% for card in cards %}
    <div class="pdf-card">
      {% if card.cropDataUri %}
      <img class="pdf-card__img" src="{{ card.cropDataUri }}" alt="">
      {% else %}
      <div class="pdf-card__placeholder">no image</div>
      {% endif %}
      <div class="pdf-card__name">{{ card.name }}</div>
      <div class="pdf-card__meta">{{ card.set }} · {{ card.number }}</div>
      <div class="pdf-card__meta">{{ card.finish }} · qty {{ card.quantity }}</div>
      <div class="pdf-card__price">{{ card.priceDisplay }}</div>
    </div>
    {% endfor %}
  </main>
</body>
</html>
```

Note on the null-crop test: `src="{{ card.cropDataUri }}"` is only emitted inside the `{% if card.cropDataUri %}` branch, so a single null-crop card renders no `src="data:` at all — the test's `not.toContain('src="data:')` holds. `card.cropDataUri` is inserted with default autoescaping; because Task 6 only ever passes a controlled `data:image/webp;base64,...` string (or `null`) there is no `|safe` and no injection surface.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/pdf.template.test.ts`
Expected: PASS — disclaimer verbatim (with `é`), stats present, data-URI img for the good card, placeholder for the null card, `<script>` escaped, no `<script>` tag or external URL anywhere.

- [ ] **Step 5: Commit**

```bash
git add web/views/collection-pdf.njk web/tests/pdf.template.test.ts
git commit -m "feat(web): print-styled collection PDF template"
```

---

### Task 6: renderCollectionPdf (Puppeteer service)

**Files:**
- Create: `web/src/lib/pdf.ts`
- Create: `web/tests/pdf.test.ts` (default unit suite — DI fake puppeteer, no browser launch)
- Create: `web/tests/pdf.render.test.ts` (gated behind `PDF_RENDER=1` — the ONE real-browser test)
- Modify: `web/package.json` (add the `puppeteer` dependency via `pnpm add`)
- Reference (do not edit): `web/views/collection-pdf.njk` (Task 5), `web/src/config.ts` (Task 4 added `upload`; Task 1 adds the `export` block consumed here as `cfg.export.page_size` / `cfg.export.render_timeout_ms`).

**Interfaces:**
- Consumes:
  - `PdfCard`, `PdfStats` types and the `collection-pdf.njk` context shape (Task 5).
  - `Config` with `cfg.export.page_size: string` and `cfg.export.render_timeout_ms: number` (Task 1 adds the `export` block; this task's tests stub it).
- Produces (per the plan's Interface Contract):
  - `export interface PdfCard { cropDataUri: string | null; name: string; set: string; number: string; finish: string; priceDisplay: string; quantity: number }`
  - `export interface PdfStats { totalCards: number; totalValueDisplay: string; generatedAt: string }`
  - `export interface PuppeteerLike` — the minimal DI seam (launch/newPage/setJavaScriptEnabled/setContent/pdf/close) the unit tests inject; the real `puppeteer` module satisfies it.
  - `export async function renderCollectionPdf(cards: PdfCard[], stats: PdfStats, cfg: Config, puppeteerImpl?: PuppeteerLike): Promise<Buffer>` — Nunjucks-renders `collection-pdf.njk`, launches Puppeteer (JS disabled per S8), `setContent(html, {waitUntil:'load', timeout})`, `page.pdf({format, printBackground:true, timeout})` → `Buffer`; browser ALWAYS closed in `finally`; throws on failure. Concurrency-1 via a module-level promise-chain mutex (two calls serialize). The optional `puppeteerImpl` parameter is the test seam — production callers omit it and get the real bundled browser.

**Design note (verified on this box — read before writing):**
- `chrome-headless-shell` is present at `~/.cache/puppeteer/chrome-headless-shell/linux-<version>/chrome-headless-shell-linux64/chrome-headless-shell`. `/usr/bin/google-chrome` exists (symlink) as a fallback. We launch with `headless: true` and let puppeteer pick its bundled `chrome-headless-shell` for determinism; if that binary is missing at runtime we fall back to `channel: 'chrome'` (system google-chrome). This fallback is expressed as: try the default launch, and on an executable-not-found launch error, relaunch with `channel: 'chrome'`. Locally the default sandbox works (non-root user); no `--no-sandbox` (that is an M5/VPS concern).

- [ ] **Step 1: Add the puppeteer dependency**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm add puppeteer`
Expected: `puppeteer` added to `package.json` dependencies; a `chrome-headless-shell` (and/or chrome) is already cached under `~/.cache/puppeteer` (present on this box), so the postinstall download is a no-op or fast.

- [ ] **Step 2: Write the failing UNIT tests (DI fake puppeteer — no real browser)**

Create `web/tests/pdf.test.ts`. A `FakePuppeteer` records every call and lets a test make `pdf()` reject or hang. Concurrency is proven by making `pdf()` await a controllable deferred and asserting the two renders never overlap (an `active` counter never exceeds 1).

```ts
import { describe, it, expect } from 'vitest';
import { renderCollectionPdf, type PuppeteerLike, type PdfCard, type PdfStats } from '../src/lib/pdf.js';
import type { Config } from '../src/config.js';

const cfg = {
  export: { page_size: 'Letter', render_timeout_ms: 30000 },
} as unknown as Config;

const cards: PdfCard[] = [
  { cropDataUri: 'data:image/webp;base64,UklGRhAA', name: 'Charizard', set: 'Base Set', number: '4/102', finish: 'holofoil', priceDisplay: '$120.00', quantity: 1 },
];
const stats: PdfStats = { totalCards: 1, totalValueDisplay: '$120.00', generatedAt: '2026-07-17 14:03 UTC' };

// A fake puppeteer recording calls. `onPdf` lets a test control the pdf() step
// (reject, or gate on a deferred to prove serialization).
function makeFake(opts: { onPdf?: () => Promise<Buffer> } = {}) {
  const calls: string[] = [];
  const setContentHtml: string[] = [];
  const pdfOptions: any[] = [];
  let jsEnabledArg: boolean | null = null;
  let closes = 0;
  let launches = 0;
  const page = {
    async setJavaScriptEnabled(v: boolean) { jsEnabledArg = v; calls.push('setJavaScriptEnabled:' + v); },
    async setContent(html: string, _o: any) { setContentHtml.push(html); calls.push('setContent'); },
    async pdf(o: any) {
      pdfOptions.push(o);
      calls.push('pdf');
      if (opts.onPdf) return opts.onPdf();
      return Buffer.from('%PDF-1.4 fake');
    },
  };
  const browser = {
    async newPage() { calls.push('newPage'); return page; },
    async close() { closes++; calls.push('close'); },
  };
  const impl: PuppeteerLike = {
    async launch(_o?: any) { launches++; calls.push('launch'); return browser as any; },
  };
  return { impl, calls, setContentHtml, pdfOptions, get jsEnabledArg() { return jsEnabledArg; }, get closes() { return closes; }, get launches() { return launches; } };
}

const DISCLAIMER =
  'NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by ' +
  'Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.';

describe('renderCollectionPdf (unit, DI fake)', () => {
  it('disables JavaScript (S8) before setting content', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.jsEnabledArg).toBe(false);
    // Order: setJavaScriptEnabled must come before setContent.
    expect(f.calls.indexOf('setJavaScriptEnabled:false')).toBeLessThan(f.calls.indexOf('setContent'));
  });

  it('renders the disclaimer HTML into setContent', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.setContentHtml[0]).toContain(DISCLAIMER);
    expect(f.setContentHtml[0]).toContain('Charizard');
  });

  it('calls page.pdf with the configured format and printBackground', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.pdfOptions[0]).toMatchObject({ format: 'Letter', printBackground: true });
  });

  it('returns the pdf Buffer', async () => {
    const f = makeFake();
    const buf = await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString('latin1')).toBe('%PDF');
  });

  it('closes the browser in finally even when pdf() rejects, and rethrows', async () => {
    const f = makeFake({ onPdf: async () => { throw new Error('boom'); } });
    await expect(renderCollectionPdf(cards, stats, cfg, f.impl)).rejects.toThrow('boom');
    expect(f.closes).toBe(1); // finally ran
  });

  it('serializes concurrent renders (concurrency-1 mutex): no overlap', async () => {
    let active = 0;
    let maxActive = 0;
    const gate: Array<() => void> = [];
    const onPdf = () =>
      new Promise<Buffer>((resolve) => {
        active++;
        maxActive = Math.max(maxActive, active);
        // Hold the render open until released, so if the mutex were broken the
        // second render would enter here concurrently and push active to 2.
        gate.push(() => { active--; resolve(Buffer.from('%PDF-1.4')); });
      });
    const f1 = makeFake({ onPdf });
    const f2 = makeFake({ onPdf });

    const p1 = renderCollectionPdf(cards, stats, cfg, f1.impl);
    const p2 = renderCollectionPdf(cards, stats, cfg, f2.impl);

    // Release both once the first has entered; a working mutex means only one
    // render is ever active, so gate never has 2 pending at once.
    const release = setInterval(() => { if (gate.length) gate.shift()!(); }, 5);
    await Promise.all([p1, p2]);
    clearInterval(release);

    expect(maxActive).toBe(1); // never two renders in flight
    expect(f1.launches).toBe(1);
    expect(f2.launches).toBe(1);
  });
});
```

- [ ] **Step 3: Run the unit tests to verify they fail**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/pdf.test.ts`
Expected: FAIL — `src/lib/pdf.ts` does not exist yet (`Cannot find module '../src/lib/pdf.js'`).

- [ ] **Step 4: Implement — create src/lib/pdf.ts**

Create `web/src/lib/pdf.ts`:

```ts
import nunjucks from 'nunjucks';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Config } from '../config.js';

export interface PdfCard {
  cropDataUri: string | null;
  name: string;
  set: string;
  number: string;
  finish: string;
  priceDisplay: string;
  quantity: number;
}
export interface PdfStats {
  totalCards: number;
  totalValueDisplay: string;
  generatedAt: string;
}

/**
 * Minimal structural type for the puppeteer module — the DI seam. The real
 * `puppeteer` default export satisfies this (launch returns a Browser with
 * newPage/close; a Page has setJavaScriptEnabled/setContent/pdf). Unit tests
 * inject a fake; production callers omit the arg and get the real browser.
 */
export interface PuppeteerLike {
  launch(opts?: Record<string, unknown>): Promise<{
    newPage(): Promise<{
      setJavaScriptEnabled(enabled: boolean): Promise<void>;
      setContent(html: string, opts?: Record<string, unknown>): Promise<void>;
      pdf(opts?: Record<string, unknown>): Promise<Buffer | Uint8Array>;
    }>;
    close(): Promise<void>;
  }>;
}

// Standalone Nunjucks env pointed at web/views — mirrors the app's autoescape:true.
// A dedicated env (not the Express one) keeps rendering usable off the request path
// (the export worker renders with no Express app in scope).
const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'views');
const njk = new nunjucks.Environment(new nunjucks.FileSystemLoader(viewsDir), {
  autoescape: true,
  noCache: true,
});

// Module-level concurrency-1 gate: a promise-chain mutex. Each render appends
// itself to the tail; the next render awaits the previous one's settlement
// (success OR failure) before starting. Puppeteer is memory-heavy; one render
// at a time bounds resource use and matches design S8 ("render is concurrency-1").
let renderChain: Promise<unknown> = Promise.resolve();

async function withMutex<T>(fn: () => Promise<T>): Promise<T> {
  const run = renderChain.then(fn, fn); // start after prior settles (ignore prior result/err)
  // Keep the chain alive but never let a rejection poison the next waiter.
  renderChain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

let defaultPuppeteer: PuppeteerLike | null = null;
async function getDefaultPuppeteer(): Promise<PuppeteerLike> {
  if (!defaultPuppeteer) {
    const mod = await import('puppeteer');
    defaultPuppeteer = (mod.default ?? mod) as unknown as PuppeteerLike;
  }
  return defaultPuppeteer;
}

/**
 * Render the collection PDF. JS disabled (S8), crops are data URIs so the browser
 * makes zero network requests, browser always closed in finally. Concurrency-1.
 * Throws on any failure (caller marks the export failed).
 */
export async function renderCollectionPdf(
  cards: PdfCard[],
  stats: PdfStats,
  cfg: Config,
  puppeteerImpl?: PuppeteerLike,
): Promise<Buffer> {
  const impl = puppeteerImpl ?? (await getDefaultPuppeteer());
  const html = njk.render('collection-pdf.njk', { cards, stats });

  return withMutex(async () => {
    let browser: Awaited<ReturnType<PuppeteerLike['launch']>> | null = null;
    try {
      browser = await launchWithFallback(impl);
      const page = await browser.newPage();
      // S8: JavaScript OFF before any content is loaded.
      await page.setJavaScriptEnabled(false);
      await page.setContent(html, {
        waitUntil: 'load',
        timeout: cfg.export.render_timeout_ms,
      });
      const out = await page.pdf({
        format: cfg.export.page_size,
        printBackground: true,
        timeout: cfg.export.render_timeout_ms,
      });
      return Buffer.isBuffer(out) ? out : Buffer.from(out);
    } finally {
      if (browser) {
        // Never let a close() failure mask the original error / swallow the result.
        await browser.close().catch(() => undefined);
      }
    }
  });
}

/**
 * Launch the bundled chrome-headless-shell for determinism; on an executable-not-found
 * launch failure, fall back to the system Chrome channel (/usr/bin/google-chrome).
 */
async function launchWithFallback(impl: PuppeteerLike) {
  try {
    return await impl.launch({ headless: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (/could not find|executable|ENOENT|browser was not found/i.test(msg)) {
      return await impl.launch({ headless: true, channel: 'chrome' });
    }
    throw err;
  }
}
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/pdf.test.ts`
Expected: PASS — JS-disabled-before-setContent, disclaimer HTML in setContent, `pdf()` got `{format:'Letter', printBackground:true}`, `%PDF` buffer returned, browser closed in `finally` even when `pdf()` rejects (and the error rethrown), and two concurrent renders never overlap (`maxActive === 1`). No real browser launched.

- [ ] **Step 6: Write the gated real-render test**

Create `web/tests/pdf.render.test.ts`. This is the ONE test that launches a real browser; it is skipped unless `PDF_RENDER=1` so the default suite never launches Chrome.

```ts
import { describe, it, expect } from 'vitest';
import { renderCollectionPdf, type PdfCard, type PdfStats } from '../src/lib/pdf.js';
import type { Config } from '../src/config.js';

// Gated: only runs with PDF_RENDER=1 (mirrors the STORAGE_INTEGRATION pattern).
const gated = process.env.PDF_RENDER === '1' ? describe : describe.skip;

const cfg = {
  export: { page_size: 'Letter', render_timeout_ms: 30000 },
} as unknown as Config;

const cards: PdfCard[] = [
  { cropDataUri: null, name: 'Charizard', set: 'Base Set', number: '4/102', finish: 'holofoil', priceDisplay: '$120.00', quantity: 1 },
  { cropDataUri: null, name: 'Pikachu', set: 'Jungle', number: '60/64', finish: 'normal', priceDisplay: '$2.50', quantity: 2 },
];
const stats: PdfStats = { totalCards: 2, totalValueDisplay: '$122.50', generatedAt: '2026-07-17 14:03 UTC' };

gated('renderCollectionPdf (real browser, PDF_RENDER=1)', () => {
  it('produces a real, non-trivial PDF buffer', async () => {
    const buf = await renderCollectionPdf(cards, stats, cfg);
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString('latin1')).toBe('%PDF'); // PDF magic
    expect(buf.length).toBeGreaterThan(1000);                    // non-trivial
  }, 60_000);
});
```

- [ ] **Step 7: Run the default suite (real test SKIPPED), then the gated test ONCE**

Default suite — the render test must be skipped:
Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run tests/pdf.test.ts tests/pdf.render.test.ts`
Expected: `pdf.test.ts` PASS; `pdf.render.test.ts` reports its suite as skipped (no browser launched).

Gated real render — run ONCE to prove a real PDF is produced:
Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && PDF_RENDER=1 pnpm vitest run tests/pdf.render.test.ts`
Expected: PASS — a real headless-Chrome render returns a `%PDF`-prefixed buffer > 1000 bytes.

- [ ] **Step 8: Run the full web suite to confirm no regression and no accidental browser launch**

Run: `export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH" && cd web && pnpm vitest run`
Expected: PASS — the whole suite green; the real-render suite skipped (PDF_RENDER unset), so no browser is launched in CI/default runs.

- [ ] **Step 9: Commit**

```bash
git add web/src/lib/pdf.ts web/tests/pdf.test.ts web/tests/pdf.render.test.ts web/package.json web/pnpm-lock.yaml
git commit -m "feat(web): Puppeteer collection PDF renderer"
```
<!-- Assembled section for docs/superpowers/plans/2026-07-17-m4-pdf-export.md — Tasks 7-9.
     Conforms to that plan's Global Constraints, File Structure, Interface Contract, and
     "Export worker behavior" prose. Consumes Task 1 (migration 004 + config.export/discord/upload),
     Task 4 (HEIC gate), Task 6 (renderCollectionPdf + PdfCard/PdfStats + the NOTBULK_STUB_PDF seam
     that Task 9 also documents). Read the plan header first. -->

### Task 7: exports queries + Node export worker

**Files:**
- Create: `web/src/queries/exports.ts`
- Create: `web/src/export-worker/jobqueue.ts`
- Create: `web/src/export-worker/worker.ts`
- Modify: `web/src/services/storage.ts` (add `get(key) -> Buffer`)
- Modify: `web/package.json:5-9` (add the `export-worker` script)
- Test: `web/tests/exports.queries.test.ts`
- Test: `web/tests/export-worker.jobqueue.test.ts`
- Test: `web/tests/export-worker.worker.test.ts`

**Interfaces:**
- Consumes:
  - `Storage.put(key, body: Buffer, contentType: string): Promise<void>` and `Storage.signedGetUrl(key): Promise<string>` (existing, `web/src/services/storage.ts`).
  - `getCollectionForExport(pool, userId, opts: CollectionFilters): Promise<CollectionRow[]>` (M3, `web/src/queries/collection.ts`) — reused unmodified; `opts` is `{}` (whole collection). Each `CollectionRow` has `crop_storage_key: string | null`, `name/set_name/number/finish: string | null`, `quantity: number`, `price_cents: number | null`, `has_price_row: boolean`.
  - `formatCents(cents: number): string` (`web/src/lib/money.ts`).
  - `renderCollectionPdf(cards: PdfCard[], stats: PdfStats, cfg: Config): Promise<Buffer>` and the `PdfCard`/`PdfStats` interfaces (Task 6, `web/src/lib/pdf.ts`). `PdfCard = { cropDataUri: string | null; name: string; set: string; number: string; finish: string; priceDisplay: string; quantity: number }`. `PdfStats = { totalCards: number; totalValueDisplay: string; generatedAt: string }`.
  - `cfg.export.{max_cards, retention_hours, storage_prefix}` and `cfg.storage.bucket` (Task 1 config additions; `Config` type extended in Task 1).
  - The `exports` table + type-partitioned `jobs.type_check` (migration 004, Task 1).
- Produces (relied on by Task 8):
  - `web/src/queries/exports.ts`:
    - `interface ExportRow { id: string; user_id: string; kind: string; status: string; storage_key: string | null; card_count: number; bytes: number; last_error: string | null; expires_at: string | null; created_at: string; updated_at: string }`
    - `createExport(pool: Pool, userId: string, kind: string): Promise<string>` — INSERT status `'queued'`, RETURNING id.
    - `getOwnedExport(pool: Pool, userId: string, exportId: string): Promise<ExportRow | null>` — owner-scoped (`WHERE id=$1 AND user_id=$2`).
    - `claimExportRow(pool: Pool, exportId: string): Promise<ExportRow | null>` — worker-side, sets `status='rendering'` and RETURNs the row (no user filter; the worker already trusts the job payload).
    - `markExportReady(pool, exportId, storageKey, bytes, cardCount, expiresAt: Date): Promise<void>`.
    - `markExportFailed(pool, exportId, error: string): Promise<void>`.
  - `web/src/export-worker/jobqueue.ts`:
    - `claimExportJob(pool: Pool, workerId: string): Promise<{ id: string; payload: { export_id: string } } | null>`
    - `completeJob(pool: Pool, jobId: string): Promise<void>`
    - `failJob(pool: Pool, jobId: string, error: string, dead: boolean): Promise<string>` — returns the terminal `status`.

---

- [ ] **Step 1: Write the failing test for `Storage.get`**

Create `web/tests/exports.queries.test.ts` with a first block covering the storage getter. `FakeStorage` in `web/tests/helpers.ts` has no `get`; the real `Storage` must gain one. Test the real class against a stubbed S3 client seam is heavy, so test the getter's contract via a thin unit that mocks the `@aws-sdk/client-s3` send. Simpler: assert the method exists and streams to a Buffer using a fake client injected on the instance.

```ts
import { describe, it, expect } from "vitest";
import { Readable } from "node:stream";
import { Storage } from "../src/services/storage.js";
import { testCfg } from "./helpers.js";

describe("Storage.get", () => {
  it("reads an object body into a Buffer", async () => {
    const storage = new Storage(testCfg as any);
    // Inject a fake S3 client: GetObjectCommand -> a body that is an async iterable stream.
    (storage as any).client = {
      send: async () => ({ Body: Readable.from([Buffer.from("hello "), Buffer.from("pdf")]) }),
    };
    const buf = await storage.get("exports/u/e.pdf");
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.toString("utf8")).toBe("hello pdf");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run tests/exports.queries.test.ts -t "reads an object body"
```
Expected: FAIL — `storage.get is not a function`.

- [ ] **Step 3: Add `get(key) -> Buffer` to `storage.ts`**

Mirror the Python `Storage.get` (`worker/notbulk/storage.py:29-31`): `GetObjectCommand` → collect the streamed body into a Buffer. The AWS SDK v3 `Body` is a Node `Readable` (async-iterable) locally against MinIO. Edit `web/src/services/storage.ts` — add the method after `put`:

```ts
  async get(key: string): Promise<Buffer> {
    const out = await this.client.send(
      new GetObjectCommand({ Bucket: this.bucket, Key: key }),
    );
    const body = out.Body as unknown as AsyncIterable<Uint8Array>;
    const chunks: Buffer[] = [];
    for await (const chunk of body) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  }
```

`GetObjectCommand` is already imported at the top of the file.

- [ ] **Step 4: Run it to verify it passes**

```bash
cd web && pnpm vitest run tests/exports.queries.test.ts -t "reads an object body"
```
Expected: PASS.

- [ ] **Step 5: Add `get` to `FakeStorage` in the test helper**

The export-worker tests inject stored crop bytes. Edit `web/tests/helpers.ts` — extend `FakeStorage` so `get(key)` returns pre-seeded bytes and unknown keys throw (mirrors a real MinIO 404):

```ts
export class FakeStorage {
  puts: Array<{ key: string; body: Buffer; contentType: string }> = [];
  objects: Map<string, Buffer> = new Map();
  photoKey(u: string, b: string, p: string) { return `${u}/${b}/${p}.webp`; }
  cropKey(u: string, b: string, c: string) { return `${u}/${b}/crops/${c}.webp`; }
  async put(key: string, body: Buffer, contentType: string) { this.puts.push({ key, body, contentType }); this.objects.set(key, body); }
  async get(key: string): Promise<Buffer> {
    const b = this.objects.get(key);
    if (!b) throw new Error(`NoSuchKey: ${key}`);
    return b;
  }
  async signedGetUrl(key: string) { return `http://127.0.0.1:9000/notbulk/${key}?sig=canned`; }
  async delete() {}
  seed(key: string, body: Buffer) { this.objects.set(key, body); }
}
```

- [ ] **Step 6: Write the failing tests for `queries/exports.ts`**

Append to `web/tests/exports.queries.test.ts`. FakePool records `{sql, params}` and returns queued rows in order.

```ts
import { FakePool } from "./helpers.js";
import { createExport, getOwnedExport, claimExportRow, markExportReady, markExportFailed } from "../src/queries/exports.js";

describe("queries/exports", () => {
  it("createExport inserts a queued row and returns its id", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1" }] });
    const id = await createExport(pool as any, "user-1", "pdf");
    expect(id).toBe("exp-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/INSERT INTO exports/i);
    expect(sql).toMatch(/'queued'/);
    expect(params).toContain("user-1");
    expect(params).toContain("pdf");
  });

  it("getOwnedExport filters by user_id and returns the row", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "ready" }] });
    const row = await getOwnedExport(pool as any, "user-1", "exp-1");
    expect(row?.id).toBe("exp-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/WHERE id=\$1 AND user_id=\$2/i);
    expect(params).toEqual(["exp-1", "user-1"]);
  });

  it("getOwnedExport returns null when not owned (0 rows)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    expect(await getOwnedExport(pool as any, "user-2", "exp-1")).toBeNull();
  });

  it("claimExportRow sets status='rendering' and returns the row", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const row = await claimExportRow(pool as any, "exp-1");
    expect(row?.status).toBe("rendering");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE exports SET status='rendering'/i);
    expect(params).toEqual(["exp-1"]);
  });

  it("markExportReady writes storage_key, bytes, card_count, status='ready', expires_at", async () => {
    const pool = new FakePool();
    const expires = new Date("2026-07-19T00:00:00Z");
    await markExportReady(pool as any, "exp-1", "exports/user-1/exp-1.pdf", 4096, 12, expires);
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE exports SET/i);
    expect(sql).toMatch(/status='ready'/);
    expect(params).toEqual(["exports/user-1/exp-1.pdf", 4096, 12, expires, "exp-1"]);
  });

  it("markExportFailed writes status='failed' + last_error", async () => {
    const pool = new FakePool();
    await markExportFailed(pool as any, "exp-1", "RenderTimeout");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/status='failed'/);
    expect(params).toEqual(["RenderTimeout", "exp-1"]);
  });
});
```

- [ ] **Step 7: Run them to verify they fail**

```bash
cd web && pnpm vitest run tests/exports.queries.test.ts
```
Expected: FAIL — `Cannot find module '../src/queries/exports.js'`.

- [ ] **Step 8: Implement `queries/exports.ts`**

Create `web/src/queries/exports.ts`. All queries are parameterised; `getOwnedExport` is owner-scoped per AC-7; `claimExportRow` is worker-side (no user filter). Ids are uuidv7.

```ts
import type { Pool } from "pg";
import { uuidv7 } from "uuidv7";

export interface ExportRow {
  id: string;
  user_id: string;
  kind: string;
  status: string;
  storage_key: string | null;
  card_count: number;
  bytes: number;
  last_error: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export async function createExport(pool: Pool, userId: string, kind: string): Promise<string> {
  const id = uuidv7();
  const { rows } = await pool.query(
    `INSERT INTO exports (id, user_id, kind, status)
     VALUES ($1, $2, $3, 'queued') RETURNING id`,
    [id, userId, kind],
  );
  return rows[0].id as string;
}

export async function getOwnedExport(
  pool: Pool,
  userId: string,
  exportId: string,
): Promise<ExportRow | null> {
  const { rows } = await pool.query(
    `SELECT id, user_id, kind, status, storage_key, card_count, bytes,
            last_error, expires_at, created_at, updated_at
       FROM exports WHERE id=$1 AND user_id=$2`,
    [exportId, userId],
  );
  return (rows[0] as ExportRow) ?? null;
}

export async function claimExportRow(pool: Pool, exportId: string): Promise<ExportRow | null> {
  const { rows } = await pool.query(
    `UPDATE exports SET status='rendering', updated_at=now()
       WHERE id=$1
     RETURNING id, user_id, kind, status, storage_key, card_count, bytes,
               last_error, expires_at, created_at, updated_at`,
    [exportId],
  );
  return (rows[0] as ExportRow) ?? null;
}

export async function markExportReady(
  pool: Pool,
  exportId: string,
  storageKey: string,
  bytes: number,
  cardCount: number,
  expiresAt: Date,
): Promise<void> {
  await pool.query(
    `UPDATE exports SET status='ready', storage_key=$1, bytes=$2, card_count=$3,
            expires_at=$4, updated_at=now()
       WHERE id=$5`,
    [storageKey, bytes, cardCount, expiresAt, exportId],
  );
}

export async function markExportFailed(pool: Pool, exportId: string, error: string): Promise<void> {
  await pool.query(
    `UPDATE exports SET status='failed', last_error=$1, updated_at=now() WHERE id=$2`,
    [error, exportId],
  );
}
```

- [ ] **Step 9: Run them to verify they pass**

```bash
cd web && pnpm vitest run tests/exports.queries.test.ts
```
Expected: PASS (all query + storage tests green).

- [ ] **Step 10: Write the failing tests for `export-worker/jobqueue.ts`**

Create `web/tests/export-worker.jobqueue.test.ts`. This mirrors the Python claim SQL (`worker/notbulk/jobqueue.py:29-58`) but is `type='export'`-only and Node-side.

```ts
import { describe, it, expect } from "vitest";
import { FakePool } from "./helpers.js";
import { claimExportJob, completeJob, failJob } from "../src/export-worker/jobqueue.js";

describe("export-worker/jobqueue", () => {
  it("claimExportJob claims a queued export job and returns id+payload", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "job-1", payload: { export_id: "exp-1" } }] });
    const claimed = await claimExportJob(pool as any, "exportw-1");
    expect(claimed).toEqual({ id: "job-1", payload: { export_id: "exp-1" } });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='running'/i);
    expect(sql).toMatch(/attempts=attempts\+1/);
    expect(sql).toMatch(/status='queued' AND run_after<=now\(\) AND type='export'/i);
    expect(sql).toMatch(/FOR UPDATE SKIP LOCKED/);
    expect(sql).toMatch(/RETURNING id, payload/);
    expect(params).toEqual(["exportw-1"]);
  });

  it("claimExportJob returns null when no job is available", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    expect(await claimExportJob(pool as any, "exportw-1")).toBeNull();
  });

  it("claimExportJob JSON-parses a string payload defensively", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "job-2", payload: '{"export_id":"exp-2"}' }] });
    const claimed = await claimExportJob(pool as any, "exportw-1");
    expect(claimed).toEqual({ id: "job-2", payload: { export_id: "exp-2" } });
  });

  it("completeJob marks the job done", async () => {
    const pool = new FakePool();
    await completeJob(pool as any, "job-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='done'/i);
    expect(params).toEqual(["job-1"]);
  });

  it("failJob(dead=true) marks the job failed and returns the terminal status", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ status: "failed" }] });
    const status = await failJob(pool as any, "job-1", "RenderTimeout", true);
    expect(status).toBe("failed");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='failed'/i);
    expect(params).toEqual(["RenderTimeout", "job-1"]);
  });
});
```

- [ ] **Step 11: Run them to verify they fail**

```bash
cd web && pnpm vitest run tests/export-worker.jobqueue.test.ts
```
Expected: FAIL — `Cannot find module '../src/export-worker/jobqueue.js'`.

- [ ] **Step 12: Implement `export-worker/jobqueue.ts`**

Create `web/src/export-worker/jobqueue.ts`. The claim SQL is the Python `_CLAIM_SQL` shape with `AND type='export'` added to the inner SELECT and a narrower RETURNING (the Node worker only needs `id, payload`). `failJob` mirrors the Python `_FAIL_DEAD_SQL`; the export worker always fails dead (no auto-retry — a rerun is a fresh export), so `dead` is accepted for signature parity but only the dead branch is exercised.

```ts
import type { Pool } from "pg";

const CLAIM_SQL =
  "UPDATE jobs SET status='running', locked_at=now(), locked_by=$1, " +
  "attempts=attempts+1, updated_at=now() " +
  "WHERE id=(SELECT id FROM jobs WHERE status='queued' AND run_after<=now() AND type='export' " +
  "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) " +
  "RETURNING id, payload";

const COMPLETE_SQL = "UPDATE jobs SET status='done', updated_at=now() WHERE id=$1";

const FAIL_DEAD_SQL =
  "UPDATE jobs SET status='failed', last_error=$1, locked_at=NULL, " +
  "locked_by=NULL, updated_at=now() WHERE id=$2 RETURNING status";

export async function claimExportJob(
  pool: Pool,
  workerId: string,
): Promise<{ id: string; payload: { export_id: string } } | null> {
  const { rows } = await pool.query(CLAIM_SQL, [workerId]);
  const row = rows[0];
  if (!row) return null;
  const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
  return { id: String(row.id), payload };
}

export async function completeJob(pool: Pool, jobId: string): Promise<void> {
  await pool.query(COMPLETE_SQL, [jobId]);
}

// The export worker always fails dead (no auto-retry; a rerun is a new export).
// `dead` is accepted for parity with the Python fail() signature.
export async function failJob(
  pool: Pool,
  jobId: string,
  error: string,
  _dead: boolean,
): Promise<string> {
  const { rows } = await pool.query(FAIL_DEAD_SQL, [error, jobId]);
  return (rows[0]?.status as string) ?? "failed";
}
```

- [ ] **Step 13: Run them to verify they pass**

```bash
cd web && pnpm vitest run tests/export-worker.jobqueue.test.ts
```
Expected: PASS.

- [ ] **Step 14: Write the failing tests for the worker's `processExportJob`**

The main loop's per-job body is extracted as a pure, injectable `processExportJob` so it can be unit-tested with fakes (no browser, no DB, no network). Create `web/tests/export-worker.worker.test.ts`.

```ts
import { describe, it, expect, vi } from "vitest";
import { FakePool, FakeStorage, testCfg } from "./helpers.js";
import { processExportJob, type ExportWorkerDeps } from "../src/export-worker/worker.js";

// A 1x1 webp-ish byte blob standing in for a crop.
const CROP = Buffer.from("webp-crop-bytes");

function makeCfg(overrides: any = {}) {
  return { ...(testCfg as any), storage: { ...(testCfg as any).storage, bucket: "notbulk" },
    export: { retention_hours: 48, render_timeout_ms: 30000, page_size: "Letter",
              storage_prefix: "exports", max_cards: 5000, ...overrides } };
}

function baseDeps(overrides: Partial<ExportWorkerDeps> = {}): ExportWorkerDeps {
  return {
    getCollectionForExport: vi.fn(async () => [
      { card_id: "c1", crop_storage_key: "user-1/b/crops/c1.webp", name: "Pikachu",
        set_name: "Base", number: "58", finish: "holofoil", quantity: 2, price_cents: 1234,
        has_price_row: true },
    ] as any),
    renderCollectionPdf: vi.fn(async () => Buffer.from("%PDF-1.4 canned")),
    ...overrides,
  };
}

describe("processExportJob", () => {
  it("happy path: claim row -> load collection -> render -> put -> markReady", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    storage.seed("user-1/b/crops/c1.webp", CROP);
    // claimExportRow returns the export row.
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    // markExportReady UPDATE (no rows needed).
    const deps = baseDeps();

    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);

    // renderCollectionPdf received a PdfCard with a data-URI crop + a formatted price.
    const [cards, stats] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards[0].cropDataUri).toBe(`data:image/webp;base64,${CROP.toString("base64")}`);
    expect(cards[0].priceDisplay).toBe("$12.34");
    expect(cards[0].quantity).toBe(2);
    expect(stats.totalCards).toBe(2); // quantity-weighted
    expect(stats.totalValueDisplay).toBe("$24.68");
    // Uploaded to exports/{user}/{export}.pdf as application/pdf.
    expect(storage.puts.at(-1)).toMatchObject({
      key: "exports/user-1/exp-1.pdf", contentType: "application/pdf",
    });
    // markExportReady ran (the last query is the UPDATE ... status='ready').
    const ready = pool.calls.find((c) => /status='ready'/.test(c.sql));
    expect(ready).toBeDefined();
    expect(ready!.params).toContain("exports/user-1/exp-1.pdf");
  });

  it("null crop_storage_key -> cropDataUri null (template renders a placeholder)", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const deps = baseDeps({
      getCollectionForExport: vi.fn(async () => [
        { card_id: "c1", crop_storage_key: null, name: "Missing", set_name: "Base",
          number: "1", finish: "normal", quantity: 1, price_cents: null, has_price_row: false } as any,
      ]),
    });
    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);
    const [cards] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards[0].cropDataUri).toBeNull();
    expect(cards[0].priceDisplay).toBe("no price data");
  });

  it("truncates to cfg.export.max_cards and logs", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const many = Array.from({ length: 3 }, (_, i) => ({
      card_id: `c${i}`, crop_storage_key: null, name: `n${i}`, set_name: "S", number: `${i}`,
      finish: "normal", quantity: 1, price_cents: null, has_price_row: false,
    }));
    const deps = baseDeps({ getCollectionForExport: vi.fn(async () => many as any) });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await processExportJob(pool as any, storage as any, makeCfg({ max_cards: 2 }), "exp-1", deps);
    const [cards] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards.length).toBe(2);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("truncated"));
    warn.mockRestore();
  });

  it("render throws -> markExportFailed with the error class, and re-throws", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const boom = new Error("chromium exploded");
    boom.name = "RenderError";
    const deps = baseDeps({ renderCollectionPdf: vi.fn(async () => { throw boom; }) });
    await expect(
      processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps),
    ).rejects.toThrow("chromium exploded");
    const failed = pool.calls.find((c) => /status='failed'/.test(c.sql));
    expect(failed).toBeDefined();
    expect(failed!.params[0]).toBe("RenderError"); // sanitized: error CLASS, not raw message
  });

  it("claimExportRow returns null (already claimed) -> no render, no throw", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [] }); // claimExportRow -> null
    const deps = baseDeps();
    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);
    expect((deps.renderCollectionPdf as any).mock.calls.length).toBe(0);
  });
});
```

- [ ] **Step 15: Run them to verify they fail**

```bash
cd web && pnpm vitest run tests/export-worker.worker.test.ts
```
Expected: FAIL — `Cannot find module '../src/export-worker/worker.js'`.

- [ ] **Step 16: Implement `export-worker/worker.ts`**

Create `web/src/export-worker/worker.ts`. `processExportJob` is the pure, testable per-export body (deps injected so tests avoid the real browser/collection query). `main` is the LISTEN + poll loop with SIGTERM graceful shutdown; it is NOT exercised by the unit suite (the E2E in Task 9 drives it end to end).

The failure policy per the plan: on any error the export row is marked `failed` (the user sees `failed` + `last_error`) AND the error re-throws so the caller (`main`) can `failJob(dead=true)` on the job row. **Discord-on-export-failure is a follow-up, not built here** — see the AMBIGUITY note at the end of this section: the Python `notify()` (Task 2, `worker/notbulk/discord.py`) is not reachable from the Node process, so the Node export worker logs the failure with `console.error` and the `exports` row's `failed` status is the user-facing signal. A Node Discord poster is deferred to M5.

```ts
import type { Pool } from "pg";
import { Client } from "pg";
import type { Config } from "../config.js";
import { Storage } from "../services/storage.js";
import { getCollectionForExport as realGetCollectionForExport } from "../queries/collection.js";
import { formatCents } from "../lib/money.js";
import { renderCollectionPdf as realRenderCollectionPdf, type PdfCard, type PdfStats } from "../lib/pdf.js";
import { claimExportRow, markExportReady, markExportFailed } from "../queries/exports.js";
import { claimExportJob, completeJob, failJob } from "./jobqueue.js";
import { randomUUID } from "node:crypto";

// Injectable seams so the per-job body is unit-testable without a browser/DB.
export interface ExportWorkerDeps {
  getCollectionForExport: typeof realGetCollectionForExport;
  renderCollectionPdf: typeof realRenderCollectionPdf;
}

const DEFAULT_DEPS: ExportWorkerDeps = {
  getCollectionForExport: realGetCollectionForExport,
  renderCollectionPdf: realRenderCollectionPdf,
};

// Row -> PdfCard priceDisplay: same rule the explorer/CSV use (null price row -> pending,
// null cents -> no price data, else formatted).
function priceDisplay(row: { has_price_row: boolean; price_cents: number | null }): string {
  if (!row.has_price_row) return "pending price";
  if (row.price_cents == null) return "no price data";
  return formatCents(row.price_cents);
}

/**
 * Render one export end to end. Marks the export row 'failed' AND re-throws on any error
 * so main() can dead-letter the job. No Discord here (see the Node/Python note in the plan).
 */
export async function processExportJob(
  pool: Pool,
  storage: Pick<Storage, "get" | "put">,
  cfg: Config,
  exportId: string,
  deps: ExportWorkerDeps = DEFAULT_DEPS,
): Promise<void> {
  const row = await claimExportRow(pool, exportId);
  if (!row) {
    // Already claimed/gone — nothing to do (idempotent; not an error).
    console.warn(`export ${exportId}: no claimable row, skipping`);
    return;
  }
  try {
    const all = await deps.getCollectionForExport(pool, row.user_id, {});
    const max = cfg.export.max_cards;
    const rows = all.length > max ? all.slice(0, max) : all;
    if (all.length > max) {
      console.warn(`export ${exportId}: collection of ${all.length} truncated to max_cards=${max}`);
    }

    const cards: PdfCard[] = [];
    for (const c of rows) {
      let cropDataUri: string | null = null;
      if (c.crop_storage_key) {
        try {
          const buf = await storage.get(c.crop_storage_key);
          cropDataUri = `data:image/webp;base64,${buf.toString("base64")}`;
        } catch (err) {
          // A missing crop object is non-fatal: render a placeholder rather than fail the whole PDF.
          console.warn(`export ${exportId}: crop ${c.crop_storage_key} unreadable, using placeholder`);
          cropDataUri = null;
        }
      }
      cards.push({
        cropDataUri,
        name: c.name ?? "Unknown",
        set: c.set_name ?? "",
        number: c.number ?? "",
        finish: c.finish ?? "",
        priceDisplay: priceDisplay(c),
        quantity: c.quantity,
      });
    }

    const totalCards = rows.reduce((n, c) => n + c.quantity, 0);
    const totalValueCents = rows.reduce(
      (n, c) => n + (c.price_cents ?? 0) * c.quantity, 0,
    );
    const stats: PdfStats = {
      totalCards,
      totalValueDisplay: formatCents(totalValueCents),
      generatedAt: new Date().toISOString(),
    };

    const buf = await deps.renderCollectionPdf(cards, stats, cfg);
    const storageKey = `${cfg.export.storage_prefix}/${row.user_id}/${exportId}.pdf`;
    await storage.put(storageKey, buf, "application/pdf");

    const expiresAt = new Date(Date.now() + cfg.export.retention_hours * 3600 * 1000);
    await markExportReady(pool, exportId, storageKey, buf.byteLength, cards.length, expiresAt);
  } catch (err) {
    const cls = (err as Error).name || "Error";
    await markExportFailed(pool, exportId, cls);
    console.error(`export ${exportId} failed:`, cls, (err as Error).message);
    throw err;
  }
}

async function runOnce(pool: Pool, storage: Storage, cfg: Config, workerId: string): Promise<boolean> {
  const job = await claimExportJob(pool, workerId);
  if (!job) return false;
  try {
    await processExportJob(pool, storage, cfg, job.payload.export_id);
    await completeJob(pool, job.id);
  } catch (err) {
    // The export row is already marked 'failed' by processExportJob; dead-letter the job row.
    await failJob(pool, job.id, (err as Error).name || "Error", true);
  }
  return true;
}

export async function main(): Promise<void> {
  const { loadConfig } = await import("../config.js");
  const { getPool } = await import("../db.js");
  const cfg = loadConfig();
  const pool = getPool();
  const storage = new Storage(cfg);
  const workerId = `export-${randomUUID()}`;

  // Dedicated LISTEN client (held open for the process lifetime — not from the pool).
  const listen = new Client({ connectionString: process.env.DATABASE_URL });
  await listen.connect();
  await listen.query("LISTEN jobs_wake");

  let running = true;
  let wake: (() => void) | null = null;
  listen.on("notification", () => { if (wake) wake(); });

  const shutdown = async () => {
    running = false;
    if (wake) wake();
    try { await listen.end(); } catch { /* ignore */ }
    try { await pool.end(); } catch { /* ignore */ }
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);

  console.log(`notbulk-export-worker ${workerId} up (LISTEN jobs_wake + 5s poll)`);
  while (running) {
    // Drain all queued export jobs, then sleep until a wake or the 5s fallback.
    let drained = false;
    while (running && (await runOnce(pool, storage, cfg, workerId))) drained = true;
    if (!running) break;
    await new Promise<void>((resolve) => {
      wake = resolve;
      const t = setTimeout(resolve, 5000);
      // Ensure the timer doesn't keep the loop from resolving twice.
      const orig = resolve;
      wake = () => { clearTimeout(t); orig(); };
    });
    void drained;
  }
}

// Entry point: only run the loop when executed directly (not when imported by tests).
const isMain = process.argv[1] && process.argv[1].endsWith("worker.ts") ||
  (process.argv[1] && process.argv[1].endsWith("worker.js"));
if (isMain) {
  main().catch((err) => {
    console.error("export-worker fatal:", err);
    process.exit(1);
  });
}
```

- [ ] **Step 17: Run the worker unit tests to verify they pass**

```bash
cd web && pnpm vitest run tests/export-worker.worker.test.ts
```
Expected: PASS (happy path, null-crop placeholder, truncation+log, render-throws→markFailed, already-claimed no-op).

- [ ] **Step 18: Add the `export-worker` script to `web/package.json`**

Edit the `scripts` block (`web/package.json:5-9`) to add the entry point. It uses `tsx` (already a devDependency) so no build step is needed locally, matching how `dev` runs `src/server.ts`:

```json
  "scripts": {
    "dev": "tsx watch src/server.ts",
    "export-worker": "tsx src/export-worker/worker.ts",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  },
```

Invoked as `pnpm --filter web export-worker` from the repo root, or `cd web && pnpm export-worker` / `cd web && pnpm tsx src/export-worker/worker.ts`. See the AMBIGUITY note on the script form.

- [ ] **Step 19: Typecheck the whole web package**

```bash
cd web && pnpm typecheck
```
Expected: no errors. (This depends on Task 6 having landed `src/lib/pdf.ts` exporting `renderCollectionPdf`, `PdfCard`, `PdfStats`, and Task 1 having extended `Config` with the `export` block.)

- [ ] **Step 20: Commit**

```bash
git add web/src/services/storage.ts web/src/queries/exports.ts \
        web/src/export-worker/jobqueue.ts web/src/export-worker/worker.ts \
        web/package.json web/tests/helpers.ts \
        web/tests/exports.queries.test.ts web/tests/export-worker.jobqueue.test.ts \
        web/tests/export-worker.worker.test.ts
git commit -m "feat(web): Node export worker rendering collection PDFs"
```

---

### Task 8: export routes + status page

**Files:**
- Create: `web/src/routes/exports.ts`
- Create: `web/views/export-status.njk`
- Modify: `web/src/services/jobs.ts` (add the `export` payload schema + `JobType`)
- Modify: `web/src/app.ts` (mount `exportsRouter`)
- Modify: `web/views/collection.njk` (add the "Export PDF" form)
- Test: `web/tests/exports.routes.test.ts`

**Interfaces:**
- Consumes:
  - `createExport`, `getOwnedExport`, `type ExportRow` (Task 7, `web/src/queries/exports.ts`).
  - `enqueue(client: PoolClient, job: EnqueueJob): Promise<string>` (M2, `web/src/services/jobs.ts`) — extended here to know the `export` type.
  - `requireUser()` and `AuthedRequest` (`web/src/middleware/session.js`).
  - `Storage.signedGetUrl(key)` (existing).
  - Transaction + NOTIFY pattern from `web/src/routes/batches.ts:93-133` (`pool.connect()` → `BEGIN` → `enqueue(client, …)` → `COMMIT` → `pool.query('NOTIFY jobs_wake')`).
- Produces:
  - `exportsRouter(pool: Pool, cfg: Config): Router` mounting three routes:
    - `POST /collection/export.pdf` → `createExport` + enqueue `export` job `{export_id}` + NOTIFY → `302 /collection/exports/:id`.
    - `GET /collection/exports/:id` → owned export → render `export-status.njk`; `404` if not owned.
    - `GET /collection/exports/:id/download` → owned + `status='ready'` + `expires_at > now` → `302` signed URL; expired → `410`; not ready → `409`; not owned → `404`.

---

- [ ] **Step 1: Write the failing test for the `export` payload schema in `jobs.ts`**

Create `web/tests/exports.routes.test.ts` (schema block first).

```ts
import { describe, it, expect } from "vitest";
import { exportPayload } from "../src/services/jobs.js";

describe("jobs export payload schema", () => {
  it("accepts { export_id } and rejects extras / missing", () => {
    expect(() => exportPayload.parse({ export_id: "exp-1" })).not.toThrow();
    expect(() => exportPayload.parse({})).toThrow();
    expect(() => exportPayload.parse({ export_id: "exp-1", extra: 1 })).toThrow();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run tests/exports.routes.test.ts -t "export payload"
```
Expected: FAIL — `exportPayload` is not exported.

- [ ] **Step 3: Add the `export` payload schema to `jobs.ts`**

Edit `web/src/services/jobs.ts`: add `'export'` to `JobType`, export the schema, and register it in `SCHEMAS`.

```ts
export type JobType = 'detect' | 'identify' | 'fetch_source' | 'ingest_correction' | 'export';

export const detectPayload = z.object({ photo_id: z.string() }).strict();
export const identifyPayload = z.object({ card_id: z.string() }).strict();
export const fetchSourcePayload = z.object({ photo_id: z.string() }).strict();
export const ingestCorrectionPayload = z
  .object({ card_id: z.string(), actual_ref_id: z.string(), predicted_ref_id: z.string().nullable() })
  .strict();
export const exportPayload = z.object({ export_id: z.string() }).strict();

const SCHEMAS: Record<JobType, z.ZodType> = {
  detect: detectPayload,
  identify: identifyPayload,
  fetch_source: fetchSourcePayload,
  ingest_correction: ingestCorrectionPayload,
  export: exportPayload,
};
```

- [ ] **Step 4: Run it to verify it passes**

```bash
cd web && pnpm vitest run tests/exports.routes.test.ts -t "export payload"
```
Expected: PASS.

- [ ] **Step 5: Write the failing tests for the routes**

Append to `web/tests/exports.routes.test.ts`. Uses `createApp` + `makeDeps` + `authedAgent`, mirroring the M2/M3 route tests. The router is mounted via `createApp` (Step 10 wires it), so these tests drive the real app.

```ts
import { createApp } from "../src/app.js";
import { makeDeps, FakePool, FakeStorage, authedAgent, testCfg } from "./helpers.js";

const USER = { id: "user-1", email: "u@test.local", tier: "free" };
const OTHER = { id: "user-2", email: "o@test.local", tier: "free" };

function future() { return new Date(Date.now() + 3600_000).toISOString(); }
function past() { return new Date(Date.now() - 3600_000).toISOString(); }

describe("POST /collection/export.pdf", () => {
  it("creates an export, enqueues an 'export' job, NOTIFYs, and 302s to the status page", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1" }] }); // createExport RETURNING id (inside the txn)
    // enqueue() INSERT ... RETURNING id
    pool.enqueue({ rows: [{ id: "job-1" }] });
    const app = createApp(makeDeps({ pool: pool as any }));
    const res = await authedAgent(app, USER).post("/collection/export.pdf");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/collection/exports/exp-1");
    // Ordering: BEGIN -> INSERT exports -> INSERT jobs (export) -> COMMIT -> NOTIFY.
    const sqls = pool.calls.map((c) => c.sql);
    const iBegin = sqls.findIndex((s) => /BEGIN/i.test(s));
    const iExport = sqls.findIndex((s) => /INSERT INTO exports/i.test(s));
    const iJob = sqls.findIndex((s) => /INSERT INTO jobs/i.test(s));
    const iCommit = sqls.findIndex((s) => /COMMIT/i.test(s));
    const iNotify = sqls.findIndex((s) => /NOTIFY jobs_wake/i.test(s));
    expect(iBegin).toBeGreaterThanOrEqual(0);
    expect(iBegin).toBeLessThan(iExport);
    expect(iExport).toBeLessThan(iJob);
    expect(iJob).toBeLessThan(iCommit);
    expect(iCommit).toBeLessThan(iNotify);
    // The job row carries type='export' and the export_id payload.
    const jobCall = pool.calls.find((c) => /INSERT INTO jobs/i.test(c.sql))!;
    expect(jobCall.params).toContain("export");
    expect(JSON.stringify(jobCall.params)).toContain("exp-1");
  });

  it("requires a user (302 to login when anon)", async () => {
    const app = createApp(makeDeps());
    const res = await (await import("supertest")).default(app).post("/collection/export.pdf");
    expect([302, 401]).toContain(res.status);
  });
});

describe("GET /collection/exports/:id (status page)", () => {
  async function renderStatus(row: any, user = USER) {
    const pool = new FakePool();
    pool.enqueue({ rows: [row] }); // getOwnedExport
    const app = createApp(makeDeps({ pool: pool as any }));
    return authedAgent(app, user).get(`/collection/exports/${row.id}`);
  }

  it("renders 'queued' with a meta-refresh and no download link", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "queued", storage_key: null, expires_at: null, last_error: null });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/http-equiv=["']?refresh/i);
    expect(res.text).not.toMatch(/\/download/);
  });

  it("renders 'rendering' with a meta-refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "rendering", storage_key: null, expires_at: null, last_error: null });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/rendering/i);
    expect(res.text).toMatch(/http-equiv=["']?refresh/i);
  });

  it("renders 'ready' with a download link and NO meta-refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: future(), last_error: null, card_count: 3, bytes: 4096 });
    expect(res.status).toBe(200);
    expect(res.text).toContain("/collection/exports/exp-1/download");
    expect(res.text).not.toMatch(/http-equiv=["']?refresh/i);
  });

  it("renders 'failed' with the last_error and no refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "failed", storage_key: null, expires_at: null, last_error: "RenderError" });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/failed/i);
    expect(res.text).toContain("RenderError");
    expect(res.text).not.toMatch(/http-equiv=["']?refresh/i);
  });

  it("404s when the export is not owned by the caller", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedExport -> null (IDOR: user-2 asks for user-1's export)
    const app = createApp(makeDeps({ pool: pool as any }));
    const res = await authedAgent(app, OTHER).get("/collection/exports/exp-1");
    expect(res.status).toBe(404);
  });
});

describe("GET /collection/exports/:id/download", () => {
  function appWith(row: any | null) {
    const pool = new FakePool();
    pool.enqueue({ rows: row ? [row] : [] }); // getOwnedExport
    return createApp(makeDeps({ pool: pool as any, storage: new FakeStorage() as any }));
  }

  it("302s to the signed URL when ready + unexpired", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: future() });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain("exports/user-1/exp-1.pdf");
    expect(res.headers.location).toContain("sig=canned");
  });

  it("410s when the export has expired", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: past() });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(410);
  });

  it("409s when not yet ready", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "rendering", storage_key: null, expires_at: null });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(409);
  });

  it("404s when not owned (IDOR on download)", async () => {
    const app = appWith(null);
    const res = await authedAgent(app, OTHER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(404);
  });
});
```

- [ ] **Step 6: Run them to verify they fail**

```bash
cd web && pnpm vitest run tests/exports.routes.test.ts
```
Expected: FAIL — routes 404 (router not mounted) / `Cannot find module '../src/routes/exports.js'`.

- [ ] **Step 7: Implement `routes/exports.ts`**

Create `web/src/routes/exports.ts`. `POST` uses the batches.ts transaction+NOTIFY pattern verbatim in shape. Download compares `expires_at` to now (a past value → `410`). Every handler owner-scopes via `getOwnedExport`.

```ts
import { Router } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { AuthedRequest } from "../middleware/session.js";
import { requireUser } from "../middleware/session.js";
import { Storage } from "../services/storage.js";
import { createExport, getOwnedExport } from "../queries/exports.js";
import { enqueue } from "../services/jobs.js";

export function exportsRouter(pool: Pool, cfg: Config, storageArg?: Storage): Router {
  const r = Router();
  const storage = storageArg ?? new Storage(cfg);

  // Kick off an async PDF export: create the row + enqueue the job in one txn, then wake a worker.
  r.post("/collection/export.pdf", requireUser(), async (req: AuthedRequest, res, next) => {
    const userId = req.user!.id;
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      const exportId = await createExport(client as unknown as Pool, userId, "pdf");
      await enqueue(client, {
        type: "export",
        payload: { export_id: exportId },
        userId,
      });
      await client.query("COMMIT");
      await pool.query("NOTIFY jobs_wake");
      return res.redirect(302, `/collection/exports/${exportId}`);
    } catch (err) {
      await client.query("ROLLBACK");
      return next(err);
    } finally {
      client.release();
    }
  });

  // Status page: queued/rendering (self-refreshing) -> ready (download) / failed (error).
  r.get("/collection/exports/:id", requireUser(), async (req: AuthedRequest, res) => {
    const row = await getOwnedExport(pool, req.user!.id, req.params.id);
    if (!row) return res.status(404).send("export not found");
    const terminal = row.status === "ready" || row.status === "failed";
    return res.render("export-status.njk", { export: row, terminal });
  });

  // Owner-checked, freshness-checked redirect to a short-lived signed MinIO URL.
  r.get("/collection/exports/:id/download", requireUser(), async (req: AuthedRequest, res) => {
    const row = await getOwnedExport(pool, req.user!.id, req.params.id);
    if (!row) return res.status(404).send("export not found");
    if (row.status !== "ready" || !row.storage_key) return res.status(409).send("export not ready");
    if (row.expires_at && new Date(row.expires_at).getTime() <= Date.now()) {
      return res.status(410).send("export expired");
    }
    const url = await storage.signedGetUrl(row.storage_key);
    return res.redirect(302, url);
  });

  return r;
}
```

- [ ] **Step 8: Create `views/export-status.njk`**

Create `web/views/export-status.njk`. CSP-safe: NO inline `<script>`; the poll is a `<meta http-equiv="refresh">` emitted into the layout's `head` block ONLY while non-terminal. Autoescaping (configured app-wide, `app.ts:47`) escapes `last_error`.

```njk
{% extends "layout.njk" %}
{% block title %}Export — NotBulk{% endblock %}

{% block head %}
  {% if not terminal %}<meta http-equiv="refresh" content="3" />{% endif %}
{% endblock %}

{% block content %}
<section class="export-status">
  <h1>PDF export</h1>

  {% if export.status == "queued" %}
    <p class="status queued">Queued — waiting for a render slot. This page refreshes automatically.</p>
  {% elif export.status == "rendering" %}
    <p class="status rendering">Rendering your collection PDF. This page refreshes automatically.</p>
  {% elif export.status == "ready" %}
    <p class="status ready">Your PDF is ready ({{ export.card_count }} cards).</p>
    <p><a class="download" href="/collection/exports/{{ export.id }}/download">Download PDF</a></p>
    <p class="hint">The download link expires after the retention window.</p>
  {% elif export.status == "failed" %}
    <p class="status failed">Export failed.</p>
    {% if export.last_error %}<p class="error">Reason: {{ export.last_error }}</p>{% endif %}
    <p><a href="/collection">Back to your collection</a></p>
  {% endif %}

  <p class="back"><a href="/collection">&larr; Collection</a></p>
</section>
{% endblock %}
```

- [ ] **Step 9: Add the "Export PDF" form to `collection.njk`**

Edit `web/views/collection.njk` — replace the single CSV export line (`:89`) with both exports side by side:

```njk
    <p class="export">
      <a href="/collection/export.csv">Download CSV</a>
      <form method="post" action="/collection/export.pdf" class="export-pdf">
        <button type="submit">Export PDF</button>
      </form>
    </p>
```

- [ ] **Step 10: Mount `exportsRouter` in `app.ts`**

Edit `web/src/app.ts`. Import the router (near the other route imports, after `collectionRouter`):

```ts
import { exportsRouter } from "./routes/exports.js";
```

Mount it at the app level right after `collectionRouter` (matching M3's app-level mount — the router applies its own `requireUser()` per route, and passes the already-constructed `storage` so tests' `FakeStorage` is used):

```ts
  app.use(collectionRouter(pool, cfg));
  app.use(exportsRouter(pool, cfg, storage));
```

`storage` is the `const storage = deps.storage ?? new Storage(cfg)` already declared at `app.ts:81`.

- [ ] **Step 11: Run the route tests to verify they pass**

```bash
cd web && pnpm vitest run tests/exports.routes.test.ts
```
Expected: PASS (POST create+enqueue+NOTIFY ordering; status page for queued/rendering/ready/failed; download 302/410/409/404; IDOR 404 on status + download).

- [ ] **Step 12: Run the full web unit suite + typecheck**

```bash
cd web && pnpm vitest run && pnpm typecheck
```
Expected: all tests PASS (Task 7 + Task 8 suites and the prior M1-M3 suites); typecheck clean.

- [ ] **Step 13: Commit**

```bash
git add web/src/routes/exports.ts web/views/export-status.njk web/views/collection.njk \
        web/src/services/jobs.ts web/src/app.ts web/tests/exports.routes.test.ts
git commit -m "feat(web): PDF export routes and status page"
```

---

### Task 9: E2E export loop + HEIC leg + finisher

**Files:**
- Create: `web/tests/e2e/export.e2e.test.ts`
- Modify: `web/src/lib/pdf.ts` (add the `NOTBULK_STUB_PDF` env seam)
- Modify: `CLAUDE.md` (M4 "Running locally" notes)
- Modify: `VERSION` (→ `0.5.0`)

**Interfaces:**
- Consumes:
  - `createApp`, `loadConfig`, `getPool`, `Storage`, `sessionMiddleware` (E2E harness, as in `web/tests/e2e/loop.e2e.test.ts` / `pricing.e2e.test.ts`).
  - `renderCollectionPdf` (Task 6) — gains a `NOTBULK_STUB_PDF` early-return seam here.
  - The `notbulk-export-worker` script (Task 7) spawned as a subprocess.
  - The HEIC gate (Task 4) — `POST /batches` must accept a HEIC upload.
- Produces:
  - A green E2E leg (behind `E2E=1`, `NOTBULK_STUB_PDF=1`), a real-render leg (behind `PDF_RENDER=1`), and the finisher (runbook + version bump).

---

- [ ] **Step 1: Add the `NOTBULK_STUB_PDF` seam to `renderCollectionPdf`**

The E2E must not require a real Chromium in CI. Add a test-only early return at the TOP of `renderCollectionPdf` in `web/src/lib/pdf.ts` — when `process.env.NOTBULK_STUB_PDF` is set it returns a canned minimal PDF Buffer and never launches the browser. Inert when unset. This is the AMBIGUITY-noted seam; it lives here (not in the worker) so the E2E exercises the REAL worker → storage → routes path with only the browser stubbed. Add immediately after the function opens, before any Puppeteer launch:

```ts
export async function renderCollectionPdf(cards: PdfCard[], stats: PdfStats, cfg: Config): Promise<Buffer> {
  // TEST-ONLY seam (documented in CLAUDE.md): NOTBULK_STUB_PDF=1 returns a canned minimal
  // PDF and skips the headless-browser launch, so the E2E export loop needs no Chromium.
  // Never set in production. The real render is covered by the PDF_RENDER=1 leg.
  if (process.env.NOTBULK_STUB_PDF) {
    return Buffer.from(
      "%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
      "latin1",
    );
  }
  // ... existing Puppeteer render (unchanged) ...
```

- [ ] **Step 2: Write the E2E export loop test**

Create `web/tests/e2e/export.e2e.test.ts`. Self-cleaning like the M2/M3 E2E tests: seeds a user/session/batch/photo/card directly, puts a crop object in MinIO, POSTs the export, spawns `notbulk-export-worker`, polls the row to `ready`, downloads via the signed URL, and asserts the bytes start with `%PDF`. Also seeds and asserts a HEIC upload leg.

```ts
// M4 acceptance gate: async PDF export loop + HEIC upload leg, against REAL local
// Postgres (5434) + MinIO (9000), with a REAL notbulk-export-worker subprocess.
// Gated on E2E=1. The export worker's browser is stubbed via NOTBULK_STUB_PDF=1 so
// no Chromium is needed in CI; the REAL render is covered by the separate PDF_RENDER
// leg (Step 6). Self-cleaning in afterAll.
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Pool } from "pg";
import { uuidv7 } from "uuidv7";
import { createHash } from "node:crypto";
import request from "supertest";
import sharp from "sharp";
import { createApp } from "../../src/app.js";
import { loadConfig } from "../../src/config.js";
import { getPool } from "../../src/db.js";
import { Storage } from "../../src/services/storage.js";
import { sessionMiddleware } from "../../src/middleware/session.js";

const RUN = process.env.E2E === "1";
const d = RUN ? describe : describe.skip;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REF_ID = "e2e-export-base1-4";

async function waitFor<T>(fn: () => Promise<T | null>, timeoutMs: number): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("waitFor timed out");
}

d("M4 e2e export loop + HEIC (real Postgres + MinIO + export worker)", () => {
  let pool: Pool;
  let worker: ChildProcess;
  let userId: string;
  let token: string;
  let batchId: string;
  let cropKey: string;
  const cleanupBatches: string[] = [];

  beforeAll(async () => {
    pool = getPool();
    const cfg = loadConfig();
    const storage = new Storage(cfg);

    await pool.query(
      `INSERT INTO card_refs (id, name, set_id, set_name, number, image_url, finishes)
         VALUES ($1,'E2E Export Card','e2e-set','E2E Set','4','https://example.invalid/c.png', ARRAY['holofoil'])
       ON CONFLICT (id) DO NOTHING`,
      [REF_ID],
    );

    userId = uuidv7();
    await pool.query(`INSERT INTO users (id, email, tier) VALUES ($1,$2,'free')`, [
      userId, `e2e-export-${userId}@test.local`,
    ]);
    const raw = uuidv7();
    token = raw;
    await pool.query(
      `INSERT INTO sessions (id, user_id, token_hash, expires_at)
         VALUES ($1,$2,$3, now() + interval '30 days')`,
      [uuidv7(), userId, createHash("sha256").update(raw).digest("hex")],
    );

    // Seed a small collection: one batch, one photo, one auto card with a crop in MinIO + a price.
    batchId = uuidv7();
    cleanupBatches.push(batchId);
    await pool.query(
      `INSERT INTO batches (id, user_id, status) VALUES ($1,$2,'complete')`,
      [batchId, userId],
    );
    const photoId = uuidv7();
    await pool.query(
      `INSERT INTO photos (id, batch_id, status, source_type, storage_key)
         VALUES ($1,$2,'done','upload',$3)`,
      [photoId, batchId, `${userId}/${batchId}/${photoId}.webp`],
    );
    const cardId = uuidv7();
    cropKey = `${userId}/${batchId}/crops/${cardId}.webp`;
    // A real WebP crop so storage.get -> data URI works end to end.
    const cropBuf = await sharp({ create: { width: 32, height: 44, channels: 3, background: "#c33" } })
      .webp().toBuffer();
    await storage.put(cropKey, cropBuf, "image/webp");
    await pool.query(
      `INSERT INTO cards (id, photo_id, card_ref_id, crop_storage_key, finish, quantity, confidence, status)
         VALUES ($1,$2,$3,$4,'holofoil',1,0.99,'auto')`,
      [cardId, photoId, REF_ID, cropKey],
    );
    await pool.query(
      `INSERT INTO prices (card_ref_id, finish, price_cents, source, fetched_at)
         VALUES ($1,'holofoil',1234,'pokemontcg', now())
       ON CONFLICT (card_ref_id, finish) DO UPDATE SET price_cents=EXCLUDED.price_cents`,
      [REF_ID],
    );

    // Spawn the REAL export worker with the browser stubbed.
    worker = spawn("pnpm", ["export-worker"], {
      cwd: path.resolve(__dirname, "../../"),
      env: { ...process.env, NOTBULK_STUB_PDF: "1" },
      stdio: "inherit",
    });
    await new Promise((r) => setTimeout(r, 2000));
  }, 30_000);

  afterAll(async () => {
    if (worker) worker.kill("SIGTERM");
    const cfg = loadConfig();
    const storage = new Storage(cfg);
    // Remove export artifacts + crops for the seeded user.
    const exps = await pool.query(`SELECT storage_key FROM exports WHERE user_id=$1`, [userId]);
    for (const e of exps.rows) if (e.storage_key) await storage.delete(e.storage_key).catch(() => {});
    await storage.delete(cropKey).catch(() => {});
    for (const b of cleanupBatches) {
      const photos = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [b]);
      const cards = await pool.query(
        `SELECT crop_storage_key FROM cards c JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`, [b]);
      for (const p of photos.rows) if (p.storage_key) await storage.delete(p.storage_key).catch(() => {});
      for (const c of cards.rows) if (c.crop_storage_key) await storage.delete(c.crop_storage_key).catch(() => {});
    }
    await pool.query(`DELETE FROM prices WHERE card_ref_id=$1`, [REF_ID]);
    await pool.query(`DELETE FROM users WHERE id=$1`, [userId]); // cascades sessions/batches/photos/cards/jobs/exports
    await pool.query(`DELETE FROM card_refs WHERE id=$1`, [REF_ID]);
    await pool.end();
  });

  it("POST export -> worker renders -> row ready -> signed download returns a PDF", async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool, cfg, storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    const post = await request(app).post("/collection/export.pdf").set("Cookie", `nb_session=${token}`);
    expect(post.status).toBe(302);
    const exportId = post.headers.location.split("/").pop()!;

    // Worker drains the 'export' job and marks the row ready.
    await waitFor(async () => {
      const r = await pool.query(`SELECT status FROM exports WHERE id=$1`, [exportId]);
      return r.rows[0]?.status === "ready" ? true : null;
    }, 60_000);

    const download = await request(app)
      .get(`/collection/exports/${exportId}/download`)
      .set("Cookie", `nb_session=${token}`);
    expect(download.status).toBe(302);
    const signed = download.headers.location;
    expect(signed).toContain("127.0.0.1:9000");

    const pdf = await fetch(signed);
    expect(pdf.ok).toBe(true);
    const bytes = Buffer.from(await pdf.arrayBuffer());
    expect(bytes.subarray(0, 4).toString("latin1")).toBe("%PDF");
  }, 120_000);

  it("HEIC upload is accepted (not a 400) and stores a WebP photo", async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool, cfg, storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    // Use the REAL committed HEIC fixture — sharp cannot WRITE heif here (and its
    // bundled libheif cannot DECODE HEVC either); the upload gate decodes it via
    // heic-convert. The fixture is a genuine heic-branded, decodable image.
    // (add `import { readFileSync } from "node:fs";` to this test file's imports)
    const heic = readFileSync(
      path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "fixtures", "sample-card.heic"),
    );
    expect(heic.subarray(4, 8).toString("latin1")).toBe("ftyp"); // sanity: real HEIC

    const create = await request(app)
      .post("/batches")
      .set("Cookie", `nb_session=${token}`)
      .attach("photos", heic, { filename: "card.heic", contentType: "image/heic" });
    expect(create.status).toBe(302); // accepted, not 400
    const heicBatchId = create.headers.location.split("/").pop()!;
    cleanupBatches.push(heicBatchId);

    // The upload gate re-encodes to WebP before the queue: the photo lands stored as .webp.
    const photo = await waitFor(async () => {
      const r = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [heicBatchId]);
      return r.rows[0]?.storage_key ? r.rows[0] : null;
    }, 30_000);
    expect(photo.storage_key).toMatch(/\.webp$/);
    const stored = await new Storage(cfg).get(photo.storage_key);
    // WebP magic: "RIFF"...."WEBP".
    expect(stored.subarray(0, 4).toString("latin1")).toBe("RIFF");
    expect(stored.subarray(8, 12).toString("latin1")).toBe("WEBP");
  }, 60_000);
});
```

- [ ] **Step 3: Confirm the default unit suite still skips the E2E**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run
```
Expected: all unit tests PASS; `export.e2e.test.ts` reported SKIPPED (no `E2E=1`). The real-render leg (Step 5) also does not run.

- [ ] **Step 4: Run the E2E once against real services (stubbed PDF)**

```bash
docker compose up -d
DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 NOTBULK_STUB_PDF=1 \
  DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  pnpm vitest run tests/e2e/export.e2e.test.ts
```
Expected: 2 tests PASS — the export loop (POST → worker → ready → signed download → `%PDF`) and the HEIC upload leg (302, stored `.webp`, `RIFF/WEBP` magic).

- [ ] **Step 5: Write the real-render leg (behind `PDF_RENDER=1`)**

The stub leg proves the plumbing; this proves the REAL Puppeteer render. Add a second describe block at the BOTTOM of `web/tests/e2e/export.e2e.test.ts`, gated on `PDF_RENDER=1` independently of `E2E`. It calls `renderCollectionPdf` directly (no worker, no DB) with a tiny in-memory card and asserts a real PDF comes back.

```ts
const RENDER = process.env.PDF_RENDER === "1";
const dr = RENDER ? describe : describe.skip;

dr("renderCollectionPdf (REAL Puppeteer, no stub)", () => {
  it("produces a real PDF buffer from one card", async () => {
    // Import lazily so the stub-only E2E never touches the browser module.
    const { renderCollectionPdf } = await import("../../src/lib/pdf.js");
    const { loadConfig } = await import("../../src/config.js");
    const cfg = loadConfig();
    const png1x1 =
      "data:image/webp;base64,UklGRhIAAABXRUJQVlA4TAYAAAAvAAAAAAfQ//73v/+BiOh/AAA=";
    const buf = await renderCollectionPdf(
      [{ cropDataUri: png1x1, name: "Pikachu", set: "Base", number: "58",
         finish: "holofoil", priceDisplay: "$12.34", quantity: 1 }],
      { totalCards: 1, totalValueDisplay: "$12.34", generatedAt: new Date().toISOString() },
      cfg,
    );
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString("latin1")).toBe("%PDF");
    expect(buf.byteLength).toBeGreaterThan(1000); // a real render, not the 60-byte stub
  }, 60_000);
});
```

- [ ] **Step 6: Run the real-render leg once**

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && PDF_RENDER=1 pnpm vitest run tests/e2e/export.e2e.test.ts -t "REAL Puppeteer"
```
Expected: 1 test PASS — a real `%PDF` buffer over 1000 bytes. (This needs the system `google-chrome` at `/usr/bin/google-chrome` per the plan header. `NOTBULK_STUB_PDF` must be UNSET.)

- [ ] **Step 7: Commit the E2E + seam**

```bash
git add web/src/lib/pdf.ts web/tests/e2e/export.e2e.test.ts
git commit -m "test(web): E2E export loop + HEIC leg with NOTBULK_STUB_PDF/PDF_RENDER seams"
```

- [ ] **Step 8: Extend the `CLAUDE.md` "Running locally" runbook with M4 notes**

Edit `CLAUDE.md`. After the M3 section, add an M4 section documenting the fourth process, the two E2E seams, and HEIC acceptance:

```markdown
## Running M4 locally (PDF export + HEIC)

A fourth process joins the M2/M3 set: the Node **export worker**, which drains `export`
jobs, renders the collection PDF (Puppeteer + system `google-chrome`), uploads it to MinIO
under `exports/{user_id}/{export_id}.pdf`, and marks the `exports` row ready.

4. Export worker (from the repo root, Node v20 on PATH):

       pnpm --filter web export-worker

   or equivalently:

       cd web && DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
         pnpm tsx src/export-worker/worker.ts

The user flow: `GET /collection` → **Export PDF** → `POST /collection/export.pdf` (creates a
queued `exports` row + an `export` job, NOTIFYs) → `/collection/exports/:id` self-refreshes
(meta-refresh, CSP-safe, no JS) until `ready`, then offers an owner-checked signed download.
Expired exports (`expires_at` past, config `export.retention_hours`, default 48) return `410`.

**HEIC uploads** are now accepted (`upload.accept_heic: true`; heic-convert (WASM) decodes them,
sharp re-encodes to WebP before the queue — the Python worker never sees HEIC).

**E2E seams (test-only, never set in production):**

- `NOTBULK_STUB_PDF=1` makes `renderCollectionPdf` return a canned minimal `%PDF` buffer and
  skip the headless-browser launch, so the export E2E needs no Chromium.
- `PDF_RENDER=1` runs the real-Puppeteer render leg of `export.e2e.test.ts` (independent of
  `E2E`; needs `google-chrome`).

### M4 export E2E

`web/tests/e2e/export.e2e.test.ts` drives POST-export → real export-worker → row `ready` →
signed download (asserts `%PDF`) plus a HEIC upload leg, against real Postgres + MinIO. Gated
on `E2E=1`; the browser is stubbed with `NOTBULK_STUB_PDF=1`:

    docker compose up -d
    DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
    cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 NOTBULK_STUB_PDF=1 \
      DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      pnpm vitest run tests/e2e/export.e2e.test.ts

Run the REAL render once separately (no `E2E` needed):

    cd web && PDF_RENDER=1 pnpm vitest run tests/e2e/export.e2e.test.ts -t "REAL Puppeteer"

The test self-cleans (deletes its seeded rows, export artifacts, and MinIO crops in `afterAll`).
```

- [ ] **Step 9: Bump `VERSION` to `0.5.0`**

M4 adds new features/files across the milestone → minor bump. Edit `VERSION`:

```
0.5.0
```

- [ ] **Step 10: Run all the green gates**

Run each gate and confirm the expected output before committing.

```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd web && pnpm vitest run          # export routes + worker + queries + pdf-template + HEIC unit tests
cd web && pnpm typecheck           # clean
cd ../worker && uv run pytest tests ../eval/tests   # discord + type-partition claim tests; eval still green
```
Expected:
- `pnpm vitest run`: all PASS; `export.e2e.test.ts` SKIPPED (no `E2E=1`); real-render leg SKIPPED (no `PDF_RENDER=1`).
- `pnpm typecheck`: no errors.
- `uv run pytest tests ../eval/tests`: all PASS (the M4 Discord + type-partitioned-claim tests plus the unchanged eval tests).

Then the two E2E legs, once each, against real services:

```bash
cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 NOTBULK_STUB_PDF=1 \
  DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
  pnpm vitest run tests/e2e/export.e2e.test.ts        # 2 pass (export loop + HEIC)
cd web && PDF_RENDER=1 pnpm vitest run tests/e2e/export.e2e.test.ts -t "REAL Puppeteer"   # 1 pass (real render)
```
Expected: the stubbed E2E reports 2 passing; the real-render leg reports 1 passing.

- [ ] **Step 11: Commit the finisher**

```bash
git add CLAUDE.md VERSION
git commit -m "docs(runbook): M4 export worker + HEIC + E2E seams; VERSION 0.5.0"
```
