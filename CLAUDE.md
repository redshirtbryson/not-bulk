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
