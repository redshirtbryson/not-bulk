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
- **Merge gate:** any change to detection, hashing, embeddings, OCR, scoring, or thresholds MUST run
  `eval/regression.py` (`cd worker && uv run python ../eval/regression.py`) and include its output;
  the suite hard-fails on any wrong auto-accept and fails on an auto-accept-rate regression.

## Services
- All Docker services bind `127.0.0.1` only. Start with `docker compose up -d`.

## Local binaries
- Project-scoped tools (e.g. `dbmate`) are downloaded to `./bin/`. `bin/` is gitignored;
  the download command lives in this file's runbook (see Task 2) — never install globally.

## Migrations (dbmate)

dbmate is a project-scoped binary at `./bin/dbmate` (`bin/` is gitignored). Install/refresh it with:

    mkdir -p bin
    curl -fsSL -o bin/dbmate \
      https://github.com/amacneil/dbmate/releases/download/v2.24.2/dbmate-linux-amd64
    chmod +x bin/dbmate

Migrations live in `migrations/` at the repo root (not dbmate's default `./db/migrations`), so
`DBMATE_MIGRATIONS_DIR=./migrations` must be set alongside `DATABASE_URL`. Run migrations under
`bws run` so `DATABASE_URL` is injected from BWS:

    bws run -- env DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up      # apply pending migrations
    bws run -- env DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate down    # roll back the last migration
    bws run -- env DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate status # list applied/pending

Migrations are raw SQL, forward-only in production, and shared by both runtimes. dbmate writes a
schema snapshot to `./db/schema.sql`, which is committed for a durable, reviewable schema record.

**Local dev bootstrap (BWS not yet configured on this box):** until the dev BWS machine token is
set up, run dbmate against the local compose Postgres directly — these are the plaintext
compose-local credentials already committed in `docker-compose.yml`, not secret material:

    DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up

Local Postgres listens on host port **5434** (5432 is a native host service, 5433 belongs to
another project) — see `docker-compose.yml`. Setting up the dev BWS token/project secrets is an
owner action item; once done, use the `bws run --` form above.

## Running M2 locally

Three processes: compose services, the Node web app, the Python worker.

Node must be v20 — set this once per shell before any `pnpm`/`node` command:

    export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"

1. Services (Postgres 5434, MinIO 9000, Mailpit 1025 / UI 8025):

       docker compose up -d
       DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
         DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up

2. Web app (http://127.0.0.1:3000):

       cd web && DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' pnpm dev

3. Worker:

       cd worker && DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' uv run notbulk-worker

Once BWS dev secrets exist, replace the inline `DATABASE_URL=` with the `bws run --` form:
`bws run -- pnpm dev` and `bws run -- uv run notbulk-worker`. Turnstile is bypassed locally
with `DEV_BYPASS_TURNSTILE=1` (logged loudly at startup); never set it in production.

### M2 end-to-end loop test

`web/tests/e2e/loop.e2e.test.ts` drives the full create -> process -> validate ->
corrections-flywheel loop against the real local Postgres + MinIO, with a real worker
subprocess (stubbed identification via `NOTBULK_STUB_IDENTIFY=1` — see the seam doc in
`worker/notbulk/handlers/identify.py`). It's gated on `E2E=1` and skipped otherwise, so
`pnpm vitest run` / `pnpm test` never runs it. Single-command form (the test spawns the
worker itself in `beforeAll`):

    docker compose up -d
    DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      DBMATE_MIGRATIONS_DIR=./migrations ./bin/dbmate up
    cd web && E2E=1 DEV_BYPASS_TURNSTILE=1 \
      DATABASE_URL='postgres://notbulk:notbulk@127.0.0.1:5434/notbulk?sslmode=disable' \
      pnpm vitest run tests/e2e/loop.e2e.test.ts

`DATABASE_URL` is exported inline in these commands, never written to a file. The test
self-cleans (deletes its seeded rows and MinIO objects in `afterAll`).

## Conventions
- Conventional commits: `feat(area):`, `fix(area):`, `docs(area):`, `chore:`. Version is not in
  the commit subject.
- Every functional commit bumps the `VERSION` file (semver, no `v` prefix):
  patch = bug fix/tweak, minor = new feature/file, major = rework/breaking/removal.
- Nothing user-supplied ever reaches a shell; image libraries are invoked directly.
