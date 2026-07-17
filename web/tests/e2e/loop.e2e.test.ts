// M2 acceptance gate: the full create -> process -> validate -> corrections
// flywheel loop, run against REAL local services (Postgres 5434, MinIO 9000)
// and a REAL worker subprocess. Gated on E2E=1 so it never runs in the normal
// unit suite / CI-less local `pnpm test`.
//
// The worker runs with NOTBULK_STUB_IDENTIFY=1 (worker/notbulk/handlers/identify.py),
// a test-only seam that skips CascadeDeps/eval-model loading and returns a canned
// high-confidence 'h'-stage Identification for every card, so this test exercises
// the real job queue, storage, SSE snapshot, and correction handler without paying
// for (or depending on) real detection/ID model accuracy.
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
const REF_ID = 'e2e-base1-4';

async function waitFor<T>(fn: () => Promise<T | null>, timeoutMs: number): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('waitFor timed out');
}

d('M2 e2e loop (real Postgres + MinIO + stubbed worker)', () => {
  let pool: Pool;
  let worker: ChildProcess;
  let userId: string;
  let token: string;
  let seededBatchIds: string[] = [];

  beforeAll(async () => {
    pool = getPool();

    // Seed a card_refs row the stub identifies to (idempotent — ON CONFLICT no-op
    // on rerun). set_id/image_url are NOT NULL in the schema; values are inert here.
    await pool.query(
      `INSERT INTO card_refs (id, name, set_id, set_name, number, image_url, finishes)
         VALUES ($1,'E2E Test Card','e2e-set','E2E Set','4','https://example.invalid/card.png', ARRAY['holofoil'])
       ON CONFLICT (id) DO NOTHING`,
      [REF_ID],
    );

    // Seed user + session directly in DB.
    userId = uuidv7();
    await pool.query(`INSERT INTO users (id, email, tier) VALUES ($1,$2,'free')`, [
      userId,
      `e2e-${userId}@test.local`,
    ]);
    const raw = uuidv7();
    token = raw;
    const tokenHash = createHash('sha256').update(raw).digest('hex');
    await pool.query(
      `INSERT INTO sessions (id, user_id, token_hash, expires_at)
         VALUES ($1,$2,$3, now() + interval '30 days')`,
      [uuidv7(), userId, tokenHash],
    );

    // Launch the worker subprocess with the stub seam.
    worker = spawn('uv', ['run', 'notbulk-worker'], {
      cwd: path.resolve(__dirname, '../../../worker'),
      env: { ...process.env, NOTBULK_STUB_IDENTIFY: '1', NOTBULK_STUB_REF_ID: REF_ID },
      stdio: 'inherit',
    });
    await new Promise((r) => setTimeout(r, 2000)); // let it connect + LISTEN
  }, 30_000);

  afterAll(async () => {
    if (worker) worker.kill('SIGTERM');

    // Self-cleaning: delete seeded rows (cascades to photos/cards/jobs/corrections)
    // and best-effort remove MinIO objects, then the reference row + user.
    const cfg = loadConfig();
    const storage = new Storage(cfg);
    for (const batchId of seededBatchIds) {
      const photos = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [batchId]);
      const cards = await pool.query(
        `SELECT crop_storage_key FROM cards c JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`,
        [batchId],
      );
      for (const p of photos.rows) {
        if (p.storage_key) await storage.delete(p.storage_key).catch(() => {});
      }
      for (const c of cards.rows) {
        if (c.crop_storage_key) await storage.delete(c.crop_storage_key).catch(() => {});
      }
    }
    await pool.query(`DELETE FROM ref_hashes WHERE card_ref_id=$1`, [REF_ID]);
    await pool.query(`DELETE FROM users WHERE id=$1`, [userId]); // cascades sessions/batches/photos/cards/jobs
    await pool.query(`DELETE FROM card_refs WHERE id=$1`, [REF_ID]);

    await pool.end();
  });

  it('create -> process -> validate -> corrections flywheel', async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool,
      cfg,
      storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    // 1. POST /batches with two JPEG fixtures.
    const create = await request(app)
      .post('/batches')
      .set('Cookie', `nb_session=${token}`)
      .attach('photos', path.join(FIX, 'card-a.jpg'))
      .attach('photos', path.join(FIX, 'card-b.jpg'));
    expect(create.status).toBe(302);
    const batchId = create.headers.location.split('/').pop()!;
    seededBatchIds.push(batchId);

    // 2. Poll DB until the batch completes.
    await waitFor(async () => {
      const r = await pool.query(`SELECT status FROM batches WHERE id=$1`, [batchId]);
      return r.rows[0]?.status === 'complete' ? true : null;
    }, 60_000);

    // 3. Photos stored in MinIO + cards have crops.
    const photos = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [batchId]);
    expect(photos.rows.length).toBe(2);
    expect(photos.rows.every((p) => p.storage_key)).toBe(true);
    const cards = await pool.query(
      `SELECT c.id, c.crop_storage_key, c.card_ref_id, c.status FROM cards c
         JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`,
      [batchId],
    );
    expect(cards.rows.length).toBeGreaterThan(0);
    expect(cards.rows.every((c) => c.crop_storage_key)).toBe(true);

    const storage = new Storage(cfg);
    for (const p of photos.rows) {
      const url = await storage.signedGetUrl(p.storage_key);
      const head = await fetch(url, { method: 'GET' });
      expect(head.ok).toBe(true);
    }

    // 4. SSE snapshot endpoint returns complete counts.
    const sse = await request(app)
      .get(`/batches/${batchId}/events`)
      .set('Cookie', `nb_session=${token}`)
      .set('x-sse-test-oneshot', '1');
    const snapFrame = sse.text.split('\n\n')[0];
    expect(snapFrame).toContain('event: snapshot');
    const snap = JSON.parse(snapFrame.split('data:')[1].trim());
    expect(snap.cards_total).toBe(cards.rows.length);

    // 5. Validate one card -> correction job runs -> ref_hashes gains 5 user_validated rows.
    const before = await pool.query(`SELECT count(*)::int AS n FROM ref_hashes WHERE source='user_validated'`);
    const target = cards.rows[0];
    const v = await request(app)
      .post(`/cards/${target.id}/validate`)
      .set('Cookie', `nb_session=${token}`)
      .type('form')
      .send({ card_ref_id: REF_ID, finish: 'holofoil' });
    expect(v.status).toBe(302);

    await waitFor(async () => {
      const r = await pool.query(`SELECT count(*)::int AS n FROM ref_hashes WHERE source='user_validated'`);
      return Number(r.rows[0].n) >= Number(before.rows[0].n) + 5 ? true : null;
    }, 30_000);
    const after = await pool.query(`SELECT count(*)::int AS n FROM ref_hashes WHERE source='user_validated'`);
    expect(Number(after.rows[0].n)).toBe(Number(before.rows[0].n) + 5);
  }, 120_000);
});
