import { describe, it, expect, vi } from 'vitest';
import request from 'supertest';
import sharp from 'sharp';
import { createApp } from '../src/app.js';
import { FakePool, FakeStorage, makeDeps } from './helpers.js';

const AUTHED = { id: 'u1', email: 'a@b.com', tier: 'free' };
const withUser = () => (req: any, _res: any, next: any) => { req.user = AUTHED; next(); };

// Ordered statement-heads from the canonical FakePool's `.calls`.
const heads = (pool: FakePool) => pool.calls.map((c) => c.sql.trim().split('\n')[0].trim());

// A real tiny JPEG so multer/route accept bytes; gateImage is faked separately.
async function jpeg() {
  return sharp({ create: { width: 32, height: 32, channels: 3, background: { r: 1, g: 2, b: 3 } } })
    .jpeg().toBuffer();
}

// Fake gate: always ok, returns a 10-byte webp.
const fakeGate = vi.fn(async () => ({ ok: true, webp: Buffer.alloc(10), width: 32, height: 32 }));

function makeApp(pool: FakePool, storage: FakeStorage, gate = fakeGate) {
  return createApp(makeDeps({
    pool,
    storage: storage as any,
    sessionMiddleware: withUser(),
    gateImage: gate as any,             // DI seam added in Step 8
    verifyTurnstile: async () => true,  // DI seam (Task 4 default in prod)
  }));
}

describe('POST /batches (upload)', () => {
  it('happy path: 302 to /batches/:id, N detect jobs, NOTIFY after COMMIT', async () => {
    const pool = new FakePool();
    // Enqueue results the route expects on the tx client, in order. The shared
    // FakeClient queue is drawn down by EVERY client.query() call, including
    // BEGIN/COMMIT/ROLLBACK (see tests/auth.test.ts convention).
    pool.enqueue({ rows: [] });                          // BEGIN
    pool.enqueue({ rows: [{ user_id: 'u1' }] });        // checkAndReserve upsert (ok)
    pool.enqueue({ rows: [{ id: 'batch-1' }] });        // INSERT batch RETURNING id
    pool.enqueue({ rows: [{ id: 'photo-1' }] });        // INSERT photo 1
    pool.enqueue({ rows: [{ id: 'job-1' }] });          // enqueue detect 1
    pool.enqueue({ rows: [{ id: 'photo-2' }] });        // INSERT photo 2
    pool.enqueue({ rows: [{ id: 'job-2' }] });          // enqueue detect 2
    pool.enqueue({ rows: [] });                          // UPDATE users.storage_bytes_used
    pool.enqueue({ rows: [] });                          // UPDATE batches.photo_count
    pool.enqueue({ rows: [] });                          // COMMIT
    const storage = new FakeStorage();
    const app = makeApp(pool, storage);

    const buf = await jpeg();
    const res = await request(app)
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .attach('photos', buf, 'a.jpg')
      .attach('photos', buf, 'b.jpg');

    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/batches/batch-1');
    expect(storage.puts.length).toBe(2);
    // Ordering: COMMIT precedes NOTIFY jobs_wake.
    const order = heads(pool);
    expect(order.indexOf('COMMIT')).toBeLessThan(order.indexOf('NOTIFY jobs_wake'));
    // Two detect jobs enqueued.
    expect(order.filter((s) => s.startsWith('INSERT INTO jobs')).length).toBe(2);
  });

  it('all files rejected → 400 with reasons, transaction rolled back, no NOTIFY', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });                  // BEGIN
    pool.enqueue({ rows: [{ user_id: 'u1' }] }); // reserve ok
    pool.enqueue({ rows: [{ id: 'batch-1' }] }); // INSERT batch
    pool.enqueue({ rows: [] });                  // ROLLBACK
    const storage = new FakeStorage();
    const gate = vi.fn(async () => ({ ok: false, reason: 'unsupported format' }));
    const app = makeApp(pool, storage, gate as any);

    const buf = await jpeg();
    const res = await request(app)
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .attach('photos', buf, 'a.jpg');

    expect(res.status).toBe(400);
    expect(res.text).toMatch(/unsupported format/);
    expect(storage.puts.length).toBe(0);
    expect(heads(pool)).toContain('ROLLBACK');
    expect(heads(pool)).not.toContain('NOTIFY jobs_wake');
  });

  it('quota exceeded → 400 naming the dimension', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });                                   // BEGIN
    pool.enqueue({ rows: [] });                                   // reserve upsert blocked
    pool.enqueue({ rows: [{ batches: 5, photos: 0, fetches: 0 }] }); // re-query
    pool.enqueue({ rows: [] });                                   // ROLLBACK
    const app = makeApp(pool, new FakeStorage());
    const buf = await jpeg();
    const res = await request(app)
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .attach('photos', buf, 'a.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/batches/);
    expect(heads(pool)).toContain('ROLLBACK');
  });

  it('multer files limit exceeded (11 files) → 400', async () => {
    const app = makeApp(new FakePool(), new FakeStorage());
    const buf = await jpeg();
    let req = request(app).post('/batches').field('cf-turnstile-response', 'x');
    for (let i = 0; i < 11; i++) req = req.attach('photos', buf, `f${i}.jpg`);
    const res = await req;
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/too many files|file limit/i);
  });
});
