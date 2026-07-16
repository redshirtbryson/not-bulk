# NotBulk - Pokemon Card Batch Scanner and Valuation Tool
## Specification v1.7 (public, Contabo VPS, agent-operated)

## 1. Overview

NotBulk is a public web application that accepts photos containing multiple Pokemon cards, detects and isolates each card, identifies it using a multi-method confidence cascade, lets the user validate uncertain results, and produces a browsable, priced collection with CSV and PDF export.

NotBulk is an unofficial fan tool. The landing page, exports, and ToS carry the standard disclaimer: not affiliated with, endorsed, or sponsored by Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokemon Company. All valuations are worded as "market value estimates," never "appraisals."

Design priority: correctness over speed. Every identification carries a confidence score. Anything below threshold goes to a human validation step rather than being guessed silently. A wrong card ID with a wrong price is worse than a slow scan.

## 2. Deployment Target

Production runs on a dedicated Contabo VPS (Debian 12, KVM). The box hosts nothing else. Blast radius by design: a full compromise of this VPS exposes only NotBulk and its four scoped secrets, and never touches home, homelab, or business infrastructure. Dev/staging runs on a homelab LXC that mirrors this layout; nothing in this spec ever grants the production box access to homelab resources.

**Base posture (applied by the provisioning script before the app exists):**

- SSH: key-only, root login disabled, password auth disabled. sshd binds to the Tailscale interface only; port 22 is closed on the public interface. Management traffic never touches the public IP.
- Public ingress exclusively through Cloudflare Tunnel (`cloudflared` as a systemd service). Zero inbound ports open on the public interface. Cloudflare WAF, bot fight mode, Turnstile, and rate limiting rules in front.
- nftables default-deny inbound. Egress allowlist only: Anthropic API, Collectr API, pokemontcg.io, Cloudflare R2, Discord webhook, Imgur (api + image CDN), Reddit (JSON endpoint + i.redd.it), Tailscale, apt/npm/pip mirrors, Cloudflare tunnel endpoints. All RFC1918 destinations denied. This is the primary post-compromise containment: a popped box cannot pivot anywhere or be repurposed as a spam/mining node.
- unattended-upgrades enabled for security patches.
- No outbound SMTP; magic-link email goes through a transactional provider (Resend or SES) over its HTTPS API.

**Stack:**

- Node 20 web layer managed by PM2
- Python 3.11 pipeline worker managed by PM2 (interpreter mode)
- PostgreSQL 16, local, listening on localhost only
- Qdrant, local, listening on localhost only
- Image storage on Cloudflare R2 (see Section 11). Local disk is scratch space only.
- Secrets injected via `bws run` under the standard systemd unit pattern, using a BWS machine token scoped to only this project's four secrets. No secrets in `ecosystem.config.cjs`, any tracked file, or shell history.
- Discord webhook notification on pipeline errors, abuse triggers, and daily usage/cost summary

**Cattle, not pets.** Contabo support is slow and their nodes are oversold, so the VPS is treated as disposable: the entire box must be reproducible from the repo's provisioning script plus BWS secrets. Nightly `pg_dump` to R2 (30 day retention). Losing the VPS costs a redeploy, never data. Benchmark a representative batch after provisioning; if CPU steal makes pipeline times unacceptable, the same scripts deploy to any other Debian VPS provider unchanged.

### 2.1 Agent-Operated Deployment

This VPS is built and operated by a Claude Code agent with SSH access. The spec therefore requires that operations be scripted, idempotent, and reversible rather than performed as one-off interactive commands:

- **Everything in the repo.** Provisioning (`provision.sh`), deploy (`deploy.sh`), nftables rules, systemd units, `cloudflared` config, PM2 ecosystem file, and nightly backup/janitor cron definitions all live in the repo. The agent applies them from the repo; it does not hand-edit live config. If the agent must change server state, it changes the script and re-runs it, so the box remains rebuildable at all times.
- **Idempotency requirement.** `provision.sh` and `deploy.sh` must be safe to run repeatedly. Deploy is: pull, install deps, run migrations (versioned, forward-only, via a migration tool, never ad-hoc SQL), restart PM2 apps, run smoke test, post result to Discord.
- **Agent guardrails.** The agent's SSH key lands on the VPS only (never any homelab host). Hard rules for the agent, stated in the repo's CLAUDE.md: never disable the firewall, sshd hardening, or the tunnel to "debug connectivity"; never print or commit secret values (reference BWS keys by name); never open a public port as a workaround; never run destructive Postgres operations outside a migration; snapshot-equivalent safety comes from the nightly dump, so confirm a fresh dump exists before schema changes.
- **Verification over trust.** Every deploy ends with a smoke test the agent must run and report: health endpoint via the public hostname (through Cloudflare), a synthetic single-photo batch through the full pipeline, and a check that ports 22/80/443 are closed on the public IP (`nmap` from the agent's side or a port-check API). A deploy without a passing smoke test is a failed deploy.
- **Audit trail.** Agent sessions operate on a non-root sudo user; auditd or at minimum persistent shell logging enabled so there is a record of what the agent did.

### 2.2 Evaluation Harness (Merge Gate)

Accuracy is the brand, and an agent iterating on the pipeline can silently degrade it, so accuracy is enforced by a regression suite, not by review alone:

- **Ground truth set:** ~100 owner-photographed cards with hand-verified labels (card ID + finish), deliberately spanning the hard cases: holos, reverse holos, sleeved cards, glare, worn vintage, upside-down and rotated cards, multi-card photos. Stored in the repo (or R2) with a manifest.
- **Regression suite:** a CLI run that pushes the ground truth set through the full cascade and asserts: zero wrong auto-accepts (hard fail), auto-accept rate >= the previous baseline (regression fail), and reports hash-tier hit rate and cost per card.
- **Merge gate:** the agent must run the suite and include its output before merging any change to detection, hashing, embeddings, OCR, scoring, or thresholds. A change that trades wrong-accept safety for hit rate is rejected by definition.
- The ground truth set grows over time with anonymized crops from real-world validation misses, so the suite gets harder as the product matures.

## 3. Architecture

```
Browser
  |  HTTPS
Node/Express web app (PM2: notbulk-web)
  |  writes jobs to Postgres, serves SSE progress stream
PostgreSQL (jobs, cards, batches, prices, corrections)
  |  polled job queue (LISTEN/NOTIFY + fallback poll)
Python worker (PM2: notbulk-worker)
  |- OpenCV detection + perspective correction
  |- Embedding match (Qdrant) against reference card images
  |- OCR (PaddleOCR) on name + collector number regions
  |- Vision LLM tiebreaker (Anthropic API, claude-haiku class model)
  |- Price lookup (Collectr API primary, pokemontcg.io fallback)
```

The web layer never blocks on pipeline work. Uploads create a batch, the worker processes cards one at a time, and the browser watches progress over Server-Sent Events. SSE over WebSockets: one-directional progress updates do not justify socket management.

## 4. Identification Pipeline (Confidence Cascade)

### 4.1 Detection and Isolation

1. Downscale copy for detection, keep original for crops.
2. Adaptive threshold + contour detection. Filter contours by area and aspect ratio (2.5:3.5, tolerance 12%).
3. Perspective warp each accepted quad to a normalized 734x1024 crop.
3a. Orientation normalization: pHash is not rotation-invariant and binder photos routinely contain upside-down or sideways cards. Test all four 90-degree orientations of each crop against a cheap orientation classifier (or the name-band OCR hit rate) and keep the upright one before any hashing or embedding.
4. If contour detection finds fewer cards than a quick YOLO pass (small fine-tuned model, or fallback to the LLM asked only "how many cards are in this image"), flag the batch for a detection review substep in validation.
5. Reject crops that fail a sharpness check (Laplacian variance below threshold). These surface in validation as "unreadable, rescan suggested" rather than producing garbage IDs.

### 4.2 Identification Methods (Cost-Ordered Cascade)

Methods run in ascending cost order. A crop exits the cascade as soon as confidence rules (4.3) are satisfied. Each method returns a candidate card ID (pokemontcg.io ID format, e.g. `sv4-123`) and a method-level score.

**Method H: Perceptual hash ensemble (near-zero cost, runs first, always).**

Three hashes per crop, compared by Hamming distance against a prebuilt index:

- Full-card DCT pHash on the grayscale normalized crop (global structure, holo-tolerant by nature since foil noise is high-frequency)
- Edge-domain pHash: same DCT hash computed on a Sobel edge map, because holo foil shifts color and luminance but not the line structure of the artwork
- Region hashes: art box, name band, and bottom text zone hashed separately, majority vote

A match requires the ensemble to agree and the top hit to clear a distance threshold with meaningful margin over the second hit. Score = inverse distance x margin x region agreement.

**Reference hash index.** Built from the same pokemontcg.io reference images as the embedding index (never scraped from TCGplayer, PriceCharting, or marketplace image libraries). At index-build time, each reference card is augmented with synthetic variants (simulated glare, rotation jitter, lighting shifts) so one card contributes multiple hash entries. Stored in Postgres with a BK-tree or linear-scan-with-SIMD lookup; at ~20k cards x augmentations this fits in memory.

**Corrections feedback loop.** When a user validates or corrects a card (6.4), the hashes of that real-world crop are appended to the index for that card ID (deduplicated by distance, capped per card). Cards that the hash tier repeatedly misses accumulate real-lighting reference hashes over time and stop missing. This tuned, user-data-augmented index is the proprietary asset; it is included in backups and never shared or exposed via any API.

**Method A: Embedding match.** CLIP-class embedding of the crop, cosine search against a Qdrant collection of all English Pokemon card reference images (bulk-downloaded once from pokemontcg.io, ~20k images, refreshed monthly per new sets). Score = cosine similarity of top hit, plus margin over second hit.

**Method B: OCR.** PaddleOCR on two fixed regions of the normalized crop: name band (top ~12%) and collector number zone (bottom-left ~15% x 8%). Parse `NNN/NNN` or promo patterns. Name + number resolved against a local card database table (synced from pokemontcg.io). Score = OCR confidence x match exactness.

**Method C: Vision LLM.** Send the crop to the Anthropic API with a constrained prompt: return JSON with name, set guess, collector number if legible, and self-reported confidence. Invoked only when H, A, and B fail to reach an accept per 4.3, to control per-scan cost. Cache by full-card pHash so re-runs are free.

### 4.3 Confidence Scoring

Composite score 0-100, evaluated at each cascade stage:

- **Stage 1 (H only):** high-margin ensemble hash match with all three hash types agreeing: base 85, plus up to 10 from margin. Auto-accept at >=90 and skip A/B/C entirely except when 4.4 finish rules apply. This is the intended fast path for the majority of clean crops.
- **Stage 2 (H + A + B):** any two of H/A/B agree on the same card ID: base 90, plus up to 10 from method scores. Auto-accept.
- H/A/B disagree, C agrees with one of them: 70-85 depending on method scores. Auto-accept at >=80, validation queue below.
- No agreement anywhere, or methods returned nothing: <=60. Always goes to validation with the top 3 candidates presented as choices.
- Thresholds configurable in `config.yaml`. Defaults: auto-accept >=80 (>=90 for hash-only), validation 40-79, "unreadable" <40.
- Per-card record stores which stage accepted it, so cost per card and hash-tier hit rate are queryable metrics.

### 4.4 Variant and Holo Handling

Collector number plus set resolves most variants, but reverse holo vs regular is frequently indistinguishable in the reference data and materially changes price. Rules:

- If the resolved card has multiple finish variants with a price spread >15%, the card is always flagged for user confirmation of finish (holo / reverse holo / non-holo), regardless of ID confidence.
- A specular-highlight heuristic (saturation and brightness variance across the art box) pre-selects the likely finish in the validation UI, but never auto-accepts it.
- Graded/slabbed cards: detect slab label region; if found, OCR the cert line and mark as graded. V1 stores grade as metadata but prices as raw (Collectr graded pricing is a V2 item).

## 5. Pricing

- Primary: Collectr API (pending access approval at getcollectr.com/api). Lookup by card ID + finish. Cache in Postgres with a 24h TTL.
- Fallback: pokemontcg.io TCGplayer market price for the matching finish key (`normal`, `holofoil`, `reverseHolofoil`).
- Every stored price records source, timestamp, and finish so the collection view can show provenance.
- If both sources miss, card shows "no price data" rather than $0.

## 6. Web App

### 6.1 Landing Page

Single page, light branding: wordmark "NotBulk", tagline "Find out what's not bulk", upload zone, sign-in. Footer links to Terms of Service and Privacy Policy (required for a public app, see 10.6). Anonymous visitors can run one trial batch (small cap); accounts unlock normal quotas.

### 6.2 Upload and URL Ingestion

Two input paths into the same pipeline:

**Direct upload:**
- Drag-and-drop or file picker, multiple images per batch (JPEG/PNG/HEIC, HEIC converted server-side).
- Client-side downscale to max 4032px long edge before upload to keep transfers sane. Server enforces its own limits regardless (10.2).

**Link ingestion (the Reddit path):**
- A paste field accepting: single Imgur image URLs, Imgur album/gallery URLs, i.redd.it image URLs, and Reddit post/gallery URLs. Appraisal posts on r/PokemonCardValue and similar already have the photos hosted; this turns "what's this binder worth" into paste-link, get-answer with zero re-uploading.
- Albums and Reddit galleries are enumerated via the Imgur API and Reddit's public JSON endpoint respectively, then each image is fetched server-side.
- Fetched images enter the exact same gate as uploads: magic-byte validation, size caps, re-encode per 10.2. A fetched image counts against the same photo quotas as an uploaded one.
- The batch record stores the source URL for provenance, which also enables a nice touch on shareable results: "scanned from this post."

**Common:**
- Cloudflare Turnstile challenge on batch creation.
- Batch caps: 10 photos per batch (uploads plus fetched images combined), 10 MB per photo, quotas per 10.4.
- Creates a batch record, redirects to the progress view.

### 6.3 Progress View

- SSE stream per batch. Displays: photos processed / total, cards detected so far, cards identified, cards queued for validation.
- Card-by-card ticker: as each card resolves, its thumbnail and name append to a running strip, so the user sees the engine working rather than a bare spinner.
- On completion, a summary bar: X auto-accepted, Y need review, Z unreadable, with a button to the validation step. Discord webhook fires here.

### 6.4 Validation Step

One card per screen, keyboard-friendly (arrow keys advance, number keys select candidates):

- Left: the isolated crop, zoomable.
- Right: top candidate with reference image side by side, plus up to 2 alternates as selectable options, plus a search box (name/number) for manual override.
- Finish selector (non-holo / reverse holo / holo) with the heuristic's guess pre-selected, shown for every card flagged per 4.4.
- "Unreadable, skip" and "Not a card" actions.
- Duplicate merge: when the same card ID + finish resolves multiple times in a batch, results collapse to one entry with a quantity counter (adjustable in validation and the explorer). Twenty copies of the same common is one row with qty 20, not twenty rows.
- Every correction is written to a `corrections` table (crop hash, wrong candidate, right answer) as future tuning data, and the validated crop's hash ensemble is appended to `ref_hashes` as a `user_validated` entry per 4.2, so the hash tier improves with use.

### 6.5 Collection Explorer

- Grid of card thumbnails with name, set, number, finish, price.
- Sort by value, name, set. Filter by set, finish, confidence source (auto vs corrected).
- Header stats: total cards, total estimated value, price data freshness.
- Batches accumulate into one collection; a batch filter allows per-scan views.

### 6.6 Export

- **CSV**: one row per card: name, set, number, finish, quantity, price, price source, price date, confidence, batch, image filename.
- **PDF**: rendered via Puppeteer from a print-styled HTML template. Cover page with collection stats, then a card grid with the actual isolated crop images (not reference images), name, set/number, finish, and price. Page footer with generation date and total value.

## 7. Data Model (Postgres)

```
users(id, email, created_at, tier, storage_bytes_used, status)
sessions(id, user_id, token_hash, expires_at)
usage(user_id, day, batches, photos, cards, llm_calls, llm_cost_cents)
batches(id, user_id, created_at, photo_count, status, expires_at, source_url)
photos(id, batch_id, r2_key, status)
cards(id, photo_id, crop_r2_key, card_ref_id, finish, quantity,
      confidence, status[auto|validated|corrected|skipped],
      method_a_id, method_a_score, method_b_id, method_b_score,
      method_c_id, method_c_score)
card_refs(id, name, set_id, set_name, number, rarity, image_url, finishes[])
prices(card_ref_id, finish, price, source, fetched_at)
corrections(id, crop_hash, predicted_ref_id, actual_ref_id, created_at)
ref_hashes(id, card_ref_id, hash_type[full|edge|region_art|region_name|region_text],
           hash_bits, source[reference|augmented|user_validated], created_at)
jobs(id, type, payload, status, attempts, created_at)
```

All IDs are UUIDv7. Images live in R2, keyed as `{user_id}/{batch_id}/{photo_id}.webp` and `{user_id}/{batch_id}/crops/{card_id}.webp`, referenced from DB. Nothing user-uploaded persists on the VPS disk or touches any homelab system.

## 8. Configuration and Secrets

- `config.yaml`: thresholds, paths, model names, price cache TTL, Discord webhook toggle.
- Secrets via Bitwarden Secrets Manager: `ANTHROPIC_API_KEY`, `COLLECTR_API_KEY`, `DISCORD_WEBHOOK_URL`, `DATABASE_URL`. Both PM2 apps launch under `bws run` per the standard systemd unit pattern.

## 9. Failure Behavior

- Worker retries a failed card 2x, then marks it unreadable with the error attached; a batch never stalls on one card.
- Anthropic or Collectr outage degrades gracefully: pipeline continues with remaining methods, affected cards land in validation instead of failing.
- All pipeline exceptions post to Discord with batch ID and card ID.
- Reference data independence: pokemontcg.io is an unofficial community API and a single point of failure for identification. The card database and reference images are mirrored locally at index-build time (bulk data dump + image set on R2), so an outage or shutdown of pokemontcg.io degrades only the pricing fallback and new-set updates, never the identification pipeline.
- Surge mode: when the LLM budget cap, quota exhaustion, or sustained load would otherwise reject new batches, the app switches to queue-and-notify: the batch is accepted, the user is told the system is busy, and an email (plus optional link back) fires when results are ready. A viral Reddit moment produces a backlog, not error pages.

## 10. Security (Public Deployment)

### 10.1 Authentication and Sessions

- Email magic-link auth. No passwords to store or breach. Links single-use, 15 minute expiry.
- Signup includes a "13 or older" attestation checkbox (COPPA posture; the audience skews young). No date-of-birth collection, which would only create data we do not want.
- Sessions are httpOnly, Secure, SameSite=Lax cookies holding an opaque token; only the SHA-256 hash is stored server-side.
- Anonymous trial: one batch of up to 3 photos, tied to a signed short-lived token, results purged after 24 hours unless the user signs up and claims the batch.
- Block disposable email domains (maintained blocklist file, same pattern as the Gmail agent blocklist).
- All authorization checks are ownership checks: every batch, photo, card, and export query filters by `user_id` from the session, never by ID alone. No sequential IDs anywhere (UUIDv7).

### 10.2 Upload Hardening

- Server-side validation by magic bytes, not extension or client MIME. Accept only JPEG, PNG, HEIC.
- Every accepted image is decoded and re-encoded server-side (libvips) to WebP before anything else touches it. This strips EXIF (including GPS), polyglot payloads, and malformed structures. The original bytes are discarded immediately after re-encode.
- Decode in a resource-limited subprocess: memory cap, CPU time cap, pixel-count cap (decompression bomb defense, reject >50 MP), 30 second timeout.
- Image processing libraries (libvips, OpenCV, Pillow if used) pinned and updated on a schedule; these are the highest-risk dependencies in the app.
- Nothing user-supplied ever reaches a shell. Worker invokes libraries directly, no `subprocess` string interpolation.

### 10.2.1 URL Fetch Hardening (SSRF Defense)

Fetching user-supplied URLs is a server-side request forgery vector by definition, so the fetcher is deliberately narrow:

- **Strict host allowlist:** `i.imgur.com`, `imgur.com`, `api.imgur.com`, `i.redd.it`, `www.reddit.com` (JSON endpoint only). Anything else is rejected before any network activity, including URLs that merely contain these strings. Parse the URL properly; compare the hostname exactly.
- **No redirect following.** Imgur and i.redd.it serve images directly; a redirect is treated as a failure. This closes the allowlisted-host-redirects-to-internal-IP hole.
- **DNS pinning:** resolve the allowlisted host, verify the resolved address is public (reject RFC1918, loopback, link-local, and metadata ranges), and connect to that verified IP. The VPS egress firewall (Section 2) is the backstop, but the fetcher does not rely on it.
- **Fetch limits:** response size hard cap (streamed, aborted at 15 MB), 20 second timeout, Content-Type must be an image type, max 10 fetches per batch, and a per-user daily fetch quota alongside 10.4.
- **Isolation:** fetches run in the worker with the same privileges as image decode, never in the web process, and fetched bytes flow straight into the 10.2 re-encode gate. Nothing fetched is ever stored or processed in original form.
- **Third-party terms:** album enumeration uses the official Imgur API with a registered client ID and honors its rate limits; Reddit enumeration uses the public `.json` endpoint with a descriptive User-Agent per their API rules. Fetched images are processed for the user who submitted the link and stored under their account like any upload; NotBulk does not crawl, bulk-harvest, or retain images beyond the user's own collection.

### 10.3 Transport and Application Layer

- TLS terminated at Cloudflare, tunnel to origin. HSTS enabled.
- Security headers: strict CSP (no inline scripts, self + R2 image host only), X-Content-Type-Options, frame-ancestors none.
- Crop and photo images served via short-lived signed R2 URLs (15 minute expiry), never public bucket paths.
- CSRF: SameSite cookies plus origin verification on all mutating requests.
- Input validation with a schema layer (zod) on every endpoint. Postgres access through parameterized queries only.
- Dependency audit (`npm audit`, `pip-audit`) in the deploy script; deploy fails on criticals.

### 10.4 Rate Limits, Quotas, and Cost Control

The vision LLM and Collectr calls make each scan cost real money, so quotas are a security control, not just politeness.

- Cloudflare edge: rate limit rule on `/api/upload` (e.g. 10 requests/min/IP) and a general burst rule.
- Application quotas (enforced in DB, per user per day): 5 batches, 50 photos, 600 cards. Configurable.
- Global daily LLM budget cap in dollars. When 80% consumed, Method C degrades to validation-queue-only (cards that would have used the LLM tiebreaker go straight to human validation). At 100%, Discord alert fires and new batches queue until the next day. The app degrades, it does not overspend.
- Per-crop LLM response cache by perceptual hash prevents replay-based cost amplification.
- Signup rate limited per IP per day.

### 10.5 Monitoring and Abuse Response

- External uptime monitoring (healthchecks.io or UptimeRobot) against the public health endpoint, because all other alerting originates from the VPS and a dead VPS is silent. Alerts to Discord and email.
- Dead-man's-switch check-ins: the nightly janitor and the nightly pg_dump each ping a healthchecks.io check on success; a missed ping alerts. A backup that silently stops running is the failure mode that hurts months later.
- Structured request logging (IP, user, route, status) with 30 day retention.
- Discord alerts: repeated upload validation failures from one IP, quota-hit spikes, LLM budget thresholds, worker crashes.
- One-command user suspension (sets `users.status`, kills sessions, halts their queued jobs).

### 10.6 Legal Surface

- Terms of Service and Privacy Policy pages. Privacy policy states: images are processed by a third-party AI API for identification, EXIF is stripped on receipt, retention periods per Section 11, deletion on request.
- Visible disclaimer on pricing: values are estimates from third-party market data, not appraisals or offers.
- Account deletion endpoint that hard-deletes the user's rows and R2 prefix.
- Data export endpoint: authenticated "download my data" returning a JSON bundle of the user's collections, cards, prices-as-shown, and image URLs (GDPR/CCPA portability, and consistent with the exports-are-the-product identity).
- ToS prohibits uploading unlawful content; the operator understands and will meet applicable reporting obligations should such content ever surface, though private storage and short retention make it unlikely.
- Pokemon/Nintendo/TPC non-affiliation disclaimer on the landing page footer and every export (see Overview).

## 11. Storage Management

Target: storage per user is bounded and predictable, and the system cleans up after itself without intervention.

### 11.1 What Is Kept, and For How Long

| Object | Format | Typical size | Retention |
|---|---|---|---|
| Original upload bytes | n/a | n/a | Deleted at re-encode (minutes) |
| Re-encoded source photo | WebP q75, max 2560px | 300-600 KB | 7 days after batch completes, then deleted. Crops are the record. |
| Card crop | WebP q80, 734x1024 | 60-120 KB | Life of the account |
| Anonymous trial data | all | - | 24 hours |
| Failed/abandoned batches | all | - | 48 hours |
| Price cache rows | Postgres | trivial | 30 day TTL sweep |
| Logs | text | - | 30 days |

Source photos are only needed for the detection-review substep and re-crops; after the 7 day window a validated collection needs only crops. A 1,000 card collection costs roughly 100 MB of R2, which is effectively free at R2 pricing and has no egress fees.

### 11.2 Quota Enforcement

- `users.storage_bytes_used` maintained transactionally on every R2 write and delete.
- Per-user cap (default 2 GB, roughly 20k cards). Uploads rejected with a clear message at cap; user can delete batches to reclaim.
- Global R2 bucket size alarm via the daily summary job; Discord alert at configured threshold.

### 11.3 Cleanup Jobs

Single nightly PM2 cron worker (`notbulk-janitor`):

1. Delete expired anonymous batches, abandoned batches, and source photos past retention (DB rows + R2 objects, R2 delete confirmed before row removal).
2. Orphan sweep: R2 objects with no DB row, and DB rows pointing at missing objects, reconciled and reported.
3. Price cache and expired session purge.
4. Post a one-line summary to Discord: bytes reclaimed, objects deleted, current bucket size, yesterday's LLM spend.

### 11.4 Local Disk

The VPS disk is scratch and system only: a bounded `/tmp/notbulk` work area cleaned per-job and on worker start. A disk-usage check refuses new jobs above 85% usage and alerts, so a cleanup bug degrades service instead of filling the root filesystem. Postgres, the hash index, and the nightly `pg_dump` staging area are the only meaningful local state, and the dump ships to R2.

## 12. Milestones

**M1 - Pipeline core (CLI):** detection, crops, hash index build with augmentation, embedding index build, OCR, cascade scoring. Run against a test batch of 50 known cards, target >=90% correct auto-accepts with zero wrong auto-accepts, and measure hash-tier hit rate (target: majority of clean crops resolve at Stage 1 without touching the LLM).

**M2 - Web app:** upload, SSE progress, validation UI, Postgres persistence.

**M3 - Pricing + explorer:** Collectr/pokemontcg.io integration, collection view, CSV export.

**M4 - PDF export + polish:** Puppeteer template, Discord hooks, HEIC support, config hardening.

**M5 - Public hardening + VPS cutover:** auth, quotas, Turnstile, R2 migration, janitor job, WAF rules, legal pages, load test of upload path. Provision the Contabo VPS from `provision.sh`, deploy via `deploy.sh`, pass the full smoke test (2.1), and benchmark a representative batch for CPU steal before pointing DNS. M1-M4 develop and run on the homelab staging LXC; nothing is public until M5 is complete.

## 13. Out of Scope (V1)

- Graded card pricing
- Non-English cards
- Condition grading from the image
- Mobile camera capture flow (upload only; a PWA capture flow is a natural V2)
- Payments/paid tiers (quota table is tier-aware so this can be added without schema changes)

## 14. V2 Roadmap

V1 proves the loop: upload, trust the IDs, get the answer, export. V2 deepens the three assets V1 creates (the corrections flywheel, the real-world crop library, and the decision moment) without drifting into being another engagement app. Ordered by strategic weight, not build order.

### 14.1 Decision Engine (monetization core)

The "found a binder" user's real question is never "what is this worth," it is "what should I do with it." V2 answers it per card:

- **Sell path:** one-click affiliate handoff per card and per batch. eBay Partner Network deep links prefilled with card name/set/number, TCGplayer affiliate links, and a "sell sheet" CSV formatted for TCGplayer mass entry. Batch action: "list everything over $X."
- **Grading ROI calculator:** for cards above a value threshold, show estimated graded values against raw, minus grading fees and shipping, with break-even math ("PSA 9 nets you +$41, PSA 8 loses $12"). Referral links to grading submission services. This is the highest-margin referral in the hobby and directly serves the binder user.
- **Keep/insure path:** documentation PDF upgrade: notarization-friendly layout, per-card photos, condition notes, price provenance, replacement-value framing, worded throughout as "market value estimate" and never "appraisal." Candidate for a one-time paid export.
- **Affiliate compliance:** eBay Partner Network and TCGplayer both require disclosure; affiliate links are labeled as such in the UI and a disclosure line appears wherever they render.
- **Decision summary view:** the collection explorer gains a triage mode that buckets the collection into sell / grade candidates / bulk, with total expected proceeds per bucket.

### 14.2 Mobile Capture PWA

Camera-first flow for the phone: guided capture (overlay grid showing where to lay cards, glare warning using the existing sharpness/specular checks live), auto-submit per photo, validation queue synced to desktop. Installable PWA, no app store. This removes the biggest V1 friction (photograph on phone, upload on desktop) while preserving the no-install pitch.

### 14.3 Condition Estimation (the crop-library payoff)

Trained on the accumulated real-world crop library plus a labeling pass: corner wear, edge whitening, surface scratches, centering estimate. Output is a conservative condition band (NM / LP / MP / HP) with visual overlays showing what the model saw, feeding directly into the Decision Engine's pricing (condition-adjusted values) and grading ROI math. Ships as clearly-labeled beta with the same philosophy as V1 identification: show confidence, never silently guess. This is the defensible feature; nothing in the competitor field does it credibly and none of them have the training data.

### 14.4 Graded Card Support

Full slab handling: cert number OCR (PSA/CGC/BGS label formats), cert verification against the graders' public lookup endpoints, graded pricing via Collectr (their graded data is a core strength) with raw pricing fallback. Slabs get a distinct card type in the explorer and exports.

### 14.5 Proprietary ID Model

Once corrections volume justifies it: fine-tune a compact vision model (or train a classifier head on the existing embedding space) on the corrections dataset plus user-validated crops. Goal is not novelty; it is replacing Method C entirely so the marginal cost of a scan approaches zero and the free tier is safely generous. Success metric: LLM invocation rate below 2% of crops with no increase in wrong auto-accepts.

### 14.6 Share and Population Features

- **Shareable collection links:** read-only public URL per collection or batch, opt-in, with values shown or hidden per owner's choice. This is the viral loop: the natural end of a Reddit "what's my binder worth" thread is posting the link.
- **Attic Index:** quarterly public report of anonymized, aggregated scan data: which sets and eras are surfacing, raw population estimates for chase cards, average binder composition. Published as content marketing; the data is exclusively first-party (what users scanned), never third-party price data.

### 14.7 Shop Mode (B2B, exploratory)

Local game stores and bulk buyers evaluate walk-in collections constantly and do it by eye. A shop-tier account: higher quotas, buylist-percentage pricing view (e.g. show 60% of market next to market), multi-collection workspaces, and per-evaluation PDF for the customer. Priced as a real subscription; shops have revenue attached to every scan. Validate demand with two or three local shops before building.

### 14.8 Explicitly Still Out of Scope

- Multi-TCG expansion (MTG, sports): the engine generalizes but the database, variants, and pricing plumbing are each their own project. Revisit only after Pokemon is undeniably won.
- Marketplace or payment handling between users: liability and scope explosion.
- Portfolio-tracking engagement features (price alerts, daily value notifications): that is Collectr's product and the engagement-app trap. One exception: an optional "reprice this collection" button with a manual refresh, consistent with the tool-not-app identity.
- Authenticity/counterfeit detection as a claim: condition beta may surface anomalies, but marketing an authenticity verdict invites liability the data cannot yet support.

### 14.9 V2 Sequencing

1. Decision Engine (revenue-bearing, mostly UI over existing data)
2. Mobile capture PWA (removes the biggest funnel friction)
3. Graded card support (small surface, high value-per-user)
4. Condition estimation beta (gated on crop library volume, target 100k+ validated crops)
5. Proprietary ID model (gated on corrections volume)
6. Shop mode (gated on organic shop signups appearing in the user base)

## 15. Acceptance Criteria (V1)

1. A photo of 9 cards on a table yields 9 isolated crops with correct perspective.
2. No card is auto-accepted with a wrong identity in the M1 test batch.
3. Any card with ambiguous finish and >15% variant price spread is presented for finish confirmation.
4. Validation of a 50-card batch takes under 5 minutes for a user with keyboard shortcuts.
5. CSV opens clean in Excel; PDF renders crop images at legible size with correct totals.
6. Killing the worker mid-batch and restarting resumes without duplicate or lost cards.
7. A user cannot access any other user's batch, card, image URL, or export by ID manipulation.
8. A crafted malformed image (bad headers, decompression bomb, polyglot) is rejected or safely re-encoded without crashing the worker or writing the original bytes anywhere.
9. Exhausting the daily LLM budget routes cards to validation instead of blocking batches or exceeding the cap.
10. The janitor demonstrably deletes expired anonymous data, abandoned batches, and aged source photos, and R2 usage stays flat over a week of trial-user churn.
11. EXIF (including GPS) is absent from every stored image.
12. A fresh Contabo VPS provisioned from `provision.sh` plus BWS secrets reaches a passing smoke test with no manual steps.
13. External scan of the VPS public IP shows zero open ports; the app is reachable only through the Cloudflare hostname; SSH answers only on Tailscale.
14. Restoring the newest nightly `pg_dump` from R2 onto a rebuilt box yields a working app with all collections intact.
15. The URL fetcher rejects non-allowlisted hosts, redirects, and hosts resolving to private/metadata IP ranges, and a pasted Imgur album or Reddit gallery link produces a correctly populated batch.
16. An upside-down card in a photo is correctly oriented and identified.
17. The regression suite runs against the ground truth set and reports zero wrong auto-accepts; the merge gate blocks a change that introduces one.
18. With pokemontcg.io unreachable, identification still works from the local mirror.
19. With the daily LLM budget exhausted, a new batch is accepted in queue-and-notify mode rather than rejected.
20. External uptime monitoring alerts within 5 minutes of the public health endpoint going dark, and a skipped nightly backup triggers a dead-man's-switch alert.
