# NotBulk — Phase 1 (Local Development) Design

Date: 2026-07-16
Status: Approved by owner
Basis: `notbulk-spec.md` v1.7, amended by four independent design reviews (CV/ML pipeline, architecture/data model, security, local-dev completeness) and owner decisions. Where this document and the spec conflict, this document wins for Phase 1; the spec remains authoritative for product intent and V2 scope.

## 1. Scope and phasing

- **Phase 1 (this design):** milestones M1–M4 from the spec, built and run entirely on the MINTY workstation (Linux Mint, 16 cores, 15 GB RAM, GTX 1070 Ti 8 GB, Docker + Compose installed).
- **Phase 2 (separate design/plan):** M5 — Contabo VPS provisioning, Cloudflare Tunnel/WAF/Turnstile, BWS production token, nftables egress allowlist, janitor cron, legal pages, cutover.
- Repo: `https://github.com/redshirtbryson/not-bulk.git`, direct pushes to `main`, `VERSION` file semver, conventional-commit prefixes.

### Owner action items (parallel with build)

1. pokemontcg.io API key (free, dev.pokemontcg.io) — gates the ~20k reference download early in M1.
2. Anthropic API key for Method C.
3. Submit the Collectr API application now so the approval clock runs.
4. Photograph the 50-card M1 test batch, growing toward the ~100-card ground-truth set (holos, reverse holos, sleeved, glare, worn vintage, upside-down/rotated, multi-card photos). Manifest format in §8.
5. At M2: Imgur client ID. Early, optional: Discord webhook so staging errors notify.

## 2. Decisions made (owner-confirmed)

| Decision | Choice |
|---|---|
| Frontend | Server-rendered Nunjucks + htmx (+ small vanilla/Alpine component for the validation screen). No bundler. Fits strict no-inline-scripts CSP. |
| Node language | TypeScript (tsc typecheck, tsx dev runner) |
| Pricing source | pokemontcg.io TCGplayer prices first; Collectr as pluggable primary behind a price-source interface, enabled by config when access is approved |
| Ground truth | Owner photographs cards in parallel with M1 code |
| Secrets | Bitwarden Secrets Manager everywhere, including local dev. Both PM2 apps and all dev commands launch under `bws run`. No `.env` files, no secrets in tracked files, ever. |
| GPU | 1070 Ti used for one-time index builds only; runtime inference is CPU (ONNX) to mirror the CPU-only VPS |

## 3. Repo layout (monorepo)

```
not-bulk/
├── VERSION
├── CLAUDE.md               # agent guardrails (security rules from spec 2.1 + this doc)
├── docker-compose.yml      # postgres:16, qdrant, minio, mailpit — all 127.0.0.1-bound
├── config.yaml             # thresholds, model names/ids, TTLs — committed, never secrets
├── migrations/             # dbmate raw-SQL, versioned, forward-only, shared by both runtimes
├── web/                    # Node 20 + TypeScript + Express + Nunjucks + htmx
├── worker/                 # Python 3.11, uv-managed pipeline
│   └── scripts/            # download_refs.py, build_hash_index.py, build_embed_index.py, bootstrap.py
├── eval/                   # regression suite CLI + committed baseline metrics
├── ground-truth/           # owner photos + manifest.json (may move to R2 if it outgrows git)
├── infra/                  # provision.sh, deploy.sh, systemd, nftables, cloudflared, egress-manifest.md
└── docs/superpowers/       # specs/ and plans/
```

Toolchain: `uv` (Python env + lockfile), `pnpm` (Node), `dbmate` (language-neutral SQL migrations), `pytest`, `vitest`. UUIDv7 generated app-side (`uuid6` package in Python, `uuidv7` in Node — Postgres 16 has no native `uuidv7()`).

**Fresh-checkout bootstrap** is a first-class, documented flow: `docker compose up -d` → `dbmate up` → `bws run -- python worker/scripts/download_refs.py` → build indexes → run eval suite. No undocumented setup steps; the same discipline the spec demands of the VPS applies to the dev box.

## 4. Identification pipeline (spec §4 with review amendments)

The cost-ordered cascade (H → A → B → C) and confidence scoring stand as specced, with these amendments:

- **A1. Zero wrong auto-accepts is a hard invariant; ≥90% auto-accept is a soft target.** When they conflict, thresholds move toward precision and the delta goes to the validation queue. The merge gate enforces the invariant; the auto-accept rate is reported, not gated, on the adversarial ground-truth set. A card whose ID accepted but whose finish was deferred to validation does **not** count as an auto-accept.
- **A2. Embedding model pinned: DINOv2 ViT-S/14** (384-dim), exported to ONNX; int8-quantized for runtime CPU inference. Zero-shot CLIP is rejected — it ranks semantically (all Charizards together), not by instance. Method A is a **shortlist generator**: it contributes candidates and agreement votes but never auto-accepts alone. The model + weights hash is pinned in `config.yaml`; the Qdrant index is model-specific and rebuilt if the model ever changes.
- **A3. Orientation:** run the full-card pHash in all four 90° rotations against the index and keep the best-scoring orientation. No separate classifier, no pre-hash OCR. Fallback (only if this proves unreliable on the ground-truth set): a tiny 4-class CNN.
- **A4. Augmentation set** (index build): small homography/perspective jitter, **WebP q80 round-trip** (index and query share the codec fingerprint), white-balance shift, mild blur, rotation jitter, and a synthetic specular sweep across the art box (targets the holo failure mode). Reference images pass through the identical preprocessing as user crops (734×1024 warp, grayscale, WebP). Augmentations are generated in-memory; only hash bits are stored. Per-card augmentation count is capped where measured false-positive rate on the ground-truth set starts climbing.
- **A5. OCR: PaddleOCR PP-OCRv4 mobile.** Name band as specced; collector number parsed by scanning the bottom third for `NNN/NNN`/promo patterns rather than a rigid box (layouts drift across eras). OCR is expected to no-op on stylized full-arts; those route onward in the cascade. If PaddleOCR proves unworkable on this stack, `easyocr` is the approved fallback.
- **A6. LLM tiebreaker cache keyed by crop content hash (SHA-256 of normalized crop bytes), not pHash** — pHash is collision-prone by design and a collision would silently serve one card's answer to another.
- **A7. Detection:** adaptive-threshold contour path first, as specced. The LLM count-check is the M1 mismatch detector. A fine-tuned YOLO detector is **deferred** until real binder/table photos show where contours fail; binder pages and overlapping cards are added to the eval set explicitly so the failure is measured, not assumed.
- **A8. Sharpness threshold** (Laplacian variance) is normalized by crop resolution and lives in `config.yaml`, tuned on the ground-truth glare/soft cases.
- **A9. `ref_hashes` is the durable source of truth**; the BK-tree/linear-SIMD lookup structure is an in-memory artifact rebuilt on worker start. User-validated and augmented entries carry their `source` tag; the per-card cap is enforced via `usage_count`/`last_matched_at` LRU eviction. Monthly reference refresh is **additive** (new sets only) and never wipes `user_validated` or `augmented` rows.
- **A10. Qdrant is backed up** (snapshot to R2/MinIO alongside the nightly pg_dump) or documented as rebuildable from the reference bundle + build script; disaster recovery must restore Method A, not just Postgres.
- **A11. Finish detection (spec §4.4) is a distinct post-identification stage** that runs after any accept, not a special case inside Stage 1. The specular heuristic's inputs are concrete: art-box ROI on the 734×1024 crop, saturation-variance and brightness-variance thresholds in `config.yaml`. It pre-selects, never auto-accepts.

## 5. Queue, contracts, and schema (architecture review amendments)

- **Q1. Job DAG:** one `detect` job per photo (inserts `cards` rows as `pending`), one `identify` job per card, plus `price` and `export` job types. Resumability falls out of idempotent per-row status transitions, satisfying acceptance criterion 6 (kill worker mid-batch, resume without loss or duplication).
- **Q2. Claiming:** `UPDATE ... WHERE id = (SELECT id FROM jobs WHERE status='queued' AND run_after <= now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`. NOTIFY is a wakeup signal only (job id at most); fallback poll every 5s bounds pickup latency; worker re-LISTENs on every reconnect.
- **Q3. Reclaim:** jobs `running` with `locked_at` older than a configurable window return to `queued` with `attempts++`, on worker start and periodically. `attempts >= max_attempts` dead-letters the job with `last_error` attached and a Discord post. Job-level `attempts` is the retry mechanism; the spec's "retries a failed card 2x" maps to `max_attempts = 3` on `identify` jobs.
- **Q4. Jobs schema:** `jobs(id, type, payload jsonb, status, attempts, max_attempts, run_after, locked_at, locked_by, last_error, batch_id, user_id, created_at, updated_at)` with an index on `(status, run_after)`. `batch_id`/`user_id` enable one-command user suspension to halt queued work.
- **Q5. Idempotent detection:** `cards.crop_index` + unique `(photo_id, crop_index)` so a retried detect job upserts rather than duplicates. This is distinct from duplicate-merge (same card ID + finish collapsing to a quantity), which operates at the batch level.
- **Q6. Cards schema gains** `method_h_id`, `method_h_score`, and `accepted_stage` (enum `h | multi | llm | validation`) — without these the spec's own headline metric (hash-tier hit rate) is unqueryable.
- **Q7. Provenance:** `source_url` and `source_type` (`upload | imgur | reddit`) move to `photos`; `batches.origin_url` optionally keeps the single "scanned from this post" headline link.
- **Q8. Anonymous trials** are `users` rows with `tier='anon'` and a token hash, giving the janitor a concrete delete predicate (`tier='anon' AND created_at < now() - interval '24 hours'`) and keeping `batches.user_id` NOT NULL. Claim-on-signup atomically flips ownership, requires the original signed token, and is one-shot.
- **Q9. Prices:** PK `(card_ref_id, finish)`, upsert on refresh. A row with `price IS NULL` is a cached known-miss (don't re-hit the API within TTL); no row means never fetched. 24h is the refetch threshold; 30d is physical row GC — two different knobs, both in config.
- **Q10. Usage:** PK `(user_id, day)`; columns include `fetches` for the URL-ingestion quota. `batches.status` enum includes `deferred` for surge mode, plus `notify_on_complete`/`notified_at` so the queue-and-notify email is representable and idempotent.
- **Q11. Web↔worker contract is an artifact:** `jobs.type` enum and per-type JSON payload schemas live in `migrations`-adjacent shared docs; zod validates on the Node side, an equivalent (pydantic) validates on dequeue in Python. The queue is a second trust boundary and is treated like one. Migrations follow expand/contract discipline so a deploy can't wedge the queue between the two apps.
- **Q12. Progress events:** worker emits `NOTIFY batch_progress` (batch id + event) on each card/photo transition; Node LISTENs and fans out to per-batch SSE subscribers with `Last-Event-ID` replay so a dropped browser connection resumes the ticker.
- **Q13. Exports are async `export` jobs**, artifact written to object storage with 48h retention and a signed download URL; renders are bounded (timeout, concurrency 1) — a 20k-card PDF never blocks a web request.
- **Q14. `storage_bytes_used`** is updated in the same DB transaction that records the object row, and the janitor recomputes it nightly from authoritative rows — Postgres and object storage are eventually consistent, not transactional, and the design says so.
- **Q15. FK/cascade rules** are explicit in migrations; account deletion follows the janitor's object-storage-before-row ordering, is idempotent, and logs completion (evidence for the GDPR/CCPA deletion claim).

## 6. Local environment substitutes

| Production | Local dev | Notes |
|---|---|---|
| Cloudflare R2 | MinIO (compose) | All storage code targets the S3 API (`@aws-sdk/client-s3` / `boto3`) with endpoint override; R2 at M5 is config, not code |
| Resend (email) | Mailpit (compose) | Magic links land in Mailpit's UI; provider interface is pluggable |
| Turnstile | Cloudflare test keys + `DEV_BYPASS_TURNSTILE` | Verification logic real; keys swapped at M5 |
| Collectr API | Fixture mock behind the price-source interface | pokemontcg.io path is fully real locally |
| `bws run` (prod machine token) | `bws run` (dev machine token) | Same injection mechanism everywhere; dev and prod tokens are separate BWS machine accounts with separately scoped secret sets |
| Cloudflare Tunnel | plain localhost | app must run without the tunnel |

**Secret inventory (BWS):** `ANTHROPIC_API_KEY`, `COLLECTR_API_KEY`, `DISCORD_WEBHOOK_URL`, `DATABASE_URL`, plus `POKEMONTCG_API_KEY` (new — the spec's four grow to five; needed for the reference download and pricing fallback), `IMGUR_CLIENT_ID` (at M2), and S3/MinIO credentials. Dev-scoped values live under the dev machine token; production values under the prod token created at M5. No secret value is ever printed, committed, or written to disk.

## 7. Security — designed in from M1/M2 (security review amendments)

These are not M5 items; they constrain code written from the first milestone. M5 activates infrastructure (tunnel, WAF, nftables, Turnstile prod keys); it does not retrofit application security.

- **S1. Ownership scoping from the first M2 endpoint:** every batch/photo/card/export query filters by `user_id` from the session. Acceptance criterion 7 becomes an M2 test, not an M5 one.
- **S2. Usage/quota accounting** increments in the same code paths that do the work, from M2.
- **S3. SSRF fetcher built fully hardened from line one:** exact-hostname allowlist; single DNS resolve; **all** returned A/AAAA records filtered against a deny-list that includes IPv6 loopback/ULA/link-local, IPv4-mapped IPv6, and metadata ranges (reject if *any* record is private — a mixed response is hostile); the connection is forced to the pinned IP (custom lookup, client re-resolution disabled); no redirects; streamed 15 MB cap, 20s timeout, image Content-Type only; every URL extracted from an Imgur/Reddit enumeration response re-runs the full gate; fan-out truncated to the batch cap **before** fetching; fetch-result cache keyed by normalized source URL so re-pasting a viral link is cheap. Acceptance criterion 15 gets a test suite with a local mock resolver in M2.
- **S4. Cost containment is per-user, not just global:** per-user daily LLM spend sub-cap and Method C invocation cap alongside the global budget. A user at their sub-cap has *their* cards routed to validation while everyone else keeps the fast path. Adversarial pHash-miss input is bounded by budget, not cache hit rate. A per-photo detected-card ceiling (config, ~30) and per-batch candidate cap bound pre-LLM CPU as decompression-style DoS defense.
- **S5. Sessions:** 30-day absolute lifetime, 7-day idle timeout, row deleted on logout/suspension, opaque token with SHA-256 stored, httpOnly/Secure/SameSite=Lax as specced.
- **S6. Magic-link rate limits:** 3/hour and 10/day per email, per-IP limits alongside, constant-time "if an account exists, we sent a link" responses, global daily outbound-email cap with Discord alert (mirrors the LLM budget pattern). Turnstile placement enumerated per unauthenticated endpoint: magic-link request, signup, anonymous batch create, link-paste. The anonymous token is bound to a solved Turnstile and is not replayable past the one-trial cap.
- **S7. Upload gate as specced (10.2)** with HEIC called out as the highest-risk decode path: libheif/libde265 pinned, decode in the resource-limited subprocess with the tightest sandbox, HEIC-specific pathologies (tile/grid bombs, derived-image chains) rejected; a malicious HEIC sample joins acceptance criterion 8's test set. Decode limits (memory, CPU time, >50 MP reject, 30s timeout) are enforced and tested locally from M1.
- **S8. PDF rendering:** template context-escapes every user-derived string (`source_url` and free-text overrides are the dangerous ones — card names resolve against the card DB but are escaped anyway); Puppeteer renders with JavaScript disabled (static print HTML needs none), sandbox on, concurrency-bounded. `source_url` is validated against the host allowlist on store and escaped on render, never rendered as a live link without re-check.
- **S9. Egress manifest as code:** `infra/egress-manifest.md` — an itemized table (host, purpose, port) updated in the same commit whenever code adds an outbound call. Day-one entries: Anthropic, pokemontcg.io, **Resend** (the review's missing-blocker), Discord, Imgur api+CDN, Reddit JSON + i.redd.it, R2, BWS API (boot-critical), Turnstile siteverify, healthchecks.io, Tailscale, apt/npm/pip, NTP/DNS. M5's nftables allowlist is generated from this file and each entry gets a smoke-test assertion; the M5 design decides the FQDN-vs-IP mechanism (leaning ipset-refresh or a tiny egress proxy).
- **S10. Discord notifications are a sink:** error classes and IDs, sanitized/truncated messages — never raw stack traces with interpolated user data, never secret values.

## 8. Evaluation harness (spec §2.2, sharpened)

- `eval/` CLI (`python -m eval.regression`) pushes the ground-truth set through the full cascade and asserts: **zero wrong auto-accepts (hard fail)**, auto-accept rate ≥ committed baseline (regression fail), and reports hash-tier hit rate, cascade **exit-stage distribution**, cost per card — **split by finish and by card era**, so holo/vintage failures can't hide in aggregates.
- Baseline metrics are committed; the merge gate (repo CLAUDE.md rule) requires suite output on any change to detection, hashing, embeddings, OCR, scoring, or thresholds.
- **Ground-truth manifest:** `ground-truth/manifest.json` — array of `{file, cards: [{card_ref_id, finish, notes}], scenario}` where `scenario` tags the hard case (holo, sleeved, glare, vintage, rotated, multi-card). The 50-card M1 test batch is a subset of, not separate from, the growing ~100-card set.
- Method C runs against the eval set use the content-hash-keyed cache (A6), so reruns are free.

## 9. Milestone plan (Phase 1)

- **M1 — Pipeline core (CLI):** compose stack, migrations, reference download + mirror, hash index build (GPU-assisted where useful), Qdrant embed index, detection→cascade→scoring as a CLI, eval harness, ground-truth manifest tooling. Gate: eval suite on the owner's test batch — zero wrong auto-accepts hard, hit-rate/auto-accept reported per §8.
- **M2 — Web app:** Express+TS+Nunjucks+htmx, magic-link auth (Mailpit), upload gate (magic bytes → libvips re-encode → WebP), URL ingestion with the full SSRF gate, job DAG + SSE progress, validation UI, ownership scoping + usage accounting, MinIO storage. Security tests for AC 7, 8, 15 land here.
- **M3 — Pricing + explorer:** price-source interface (pokemontcg.io real, Collectr mock), collection explorer, CSV export, per-user quotas and LLM sub-caps live.
- **M4 — PDF export + polish:** Puppeteer (hardened per S8) as async export jobs, Discord hooks, HEIC support (per S7), janitor job (runs locally against MinIO/Postgres), config hardening.
- Exit criteria for Phase 1: all spec acceptance criteria that don't require the VPS/Cloudflare (i.e., all except 12, 13, 14, 20) pass locally.

## 10. Testing strategy

TDD per milestone: pytest for the worker (unit + pipeline integration against fixture images), vitest + supertest for web endpoints (ownership, quotas, validation flows), the eval harness as the accuracy regression net, and adversarial fixtures for the security gates (malformed images, HEIC bombs, SSRF mock resolver, IDOR attempts). CI can run everything except GPU index builds.
