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
