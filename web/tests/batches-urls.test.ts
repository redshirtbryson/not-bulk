import { describe, it, expect } from 'vitest';
import request from 'supertest';
import { createApp } from '../src/app.js';
import { FakePool, FakeStorage, makeDeps } from './helpers.js';

const AUTHED = { id: 'u1', email: 'a@b.com', tier: 'free' };
const withUser = () => (req: any, _res: any, next: any) => { req.user = AUTHED; next(); };

// Ordered statement-heads from the canonical FakePool's `.calls`.
const heads = (pool: FakePool) => pool.calls.map((c) => c.sql.trim().split('\n')[0].trim());

function makeApp(pool: FakePool) {
  return createApp(makeDeps({
    pool,
    storage: new FakeStorage() as any,
    sessionMiddleware: withUser(),
    verifyTurnstile: async () => true,
  }));
}

function poolForNUrls(n: number) {
  const pool = new FakePool();
  pool.enqueue({ rows: [] });                  // BEGIN
  pool.enqueue({ rows: [{ user_id: 'u1' }] }); // reserve ok
  pool.enqueue({ rows: [{ id: 'batch-1' }] }); // INSERT batch
  for (let i = 0; i < n; i++) {
    pool.enqueue({ rows: [{ id: `photo-${i}` }] }); // INSERT photo
    pool.enqueue({ rows: [{ id: `job-${i}` }] });    // enqueue fetch_source
  }
  pool.enqueue({ rows: [] }); // UPDATE batches.photo_count
  return pool;
}

describe('POST /batches (urls)', () => {
  it('accepts a single imgur direct link → 302, one fetching photo + fetch_source job', async () => {
    const pool = poolForNUrls(1);
    const res = await request(makeApp(pool))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://i.imgur.com/abc123.jpg');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/batches/batch-1');
    expect(heads(pool).filter((s) => s.startsWith('INSERT INTO jobs')).length).toBe(1);
    expect(heads(pool).indexOf('COMMIT')).toBeLessThan(heads(pool).indexOf('NOTIFY jobs_wake'));
  });

  it('accepts a reddit gallery URL', async () => {
    const pool = poolForNUrls(1);
    const res = await request(makeApp(pool))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://www.reddit.com/r/pkmntcgcollections/comments/abc/mybinder/');
    expect(res.status).toBe(302);
  });

  it('rejects a non-allowlisted host', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://example.com/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  it('rejects the suffix-bypass shape evil-imgur.com', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://evil-imgur.com/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  it('rejects the subdomain-bypass shape imgur.com.evil.io', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://imgur.com.evil.io/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  // Classic SSRF-allowlist bypass shapes: correct by construction (exact
  // hostname membership), pinned so a careless endsWith/includes refactor fails.
  it('rejects the userinfo-trick shape imgur.com@evil.com', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://imgur.com@evil.com/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  it('rejects a non-allowlisted subdomain sub.imgur.com', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://sub.imgur.com/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  it('rejects a URL with an allowed host only in the path/query', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://example.com/i.imgur.com?x=imgur.com');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/host not allowed/i);
  });

  it('rejects http:// (https only)', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'http://i.imgur.com/x.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/https/i);
  });

  it('rejects more than 10 URLs', async () => {
    const many = Array.from({ length: 11 }, (_, i) => `https://i.imgur.com/a${i}.jpg`).join('\n');
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', many);
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/too many urls/i);
  });

  it('rejects when the fetch quota is exceeded (names fetches)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });                                    // BEGIN
    pool.enqueue({ rows: [] });                                    // reserve blocked
    pool.enqueue({ rows: [{ batches: 0, photos: 0, fetches: 20 }] }); // re-query
    const res = await request(makeApp(pool))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://i.imgur.com/a.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/fetches/);
    expect(heads(pool)).toContain('ROLLBACK');
  });

  it('rejects mixed multipart + urls in one request', async () => {
    const res = await request(makeApp(new FakePool()))
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://i.imgur.com/a.jpg')
      .attach('photos', Buffer.from([0xff, 0xd8, 0xff, 0x00]), 'a.jpg');
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/choose one input method/i);
  });
});
