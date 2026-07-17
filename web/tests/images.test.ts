import { describe, it, expect } from 'vitest';
import request from 'supertest';
import { createApp } from '../src/app.js';
import { FakePool, FakeStorage, makeDeps } from './helpers.js';

// Stub session middleware: force a fixed authed user for these route tests.
const AUTHED_USER = { id: 'u1', email: 'a@b.com', tier: 'free' };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

// FakeStorage (canonical, from helpers.ts) records `puts` and returns a canned signed URL;
// `signedGetUrl` echoes the key into the URL, so `.puts`/URL suffice to assert the key used.

describe('GET /img/photo/:id', () => {
  it('302 → signed URL when the photo is owned and stored', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'p1', storage_key: 'u1/b1/p1.webp' }] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get('/img/photo/p1');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe(
      'http://127.0.0.1:9000/notbulk/u1/b1/p1.webp?sig=canned',
    );
    expect(res.headers.location).toContain('u1/b1/p1.webp');
  });

  it('404 when the photo is not owned (query returns no row) — indistinguishable from missing', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get('/img/photo/p1');
    expect(res.status).toBe(404);
  });

  it('404 when the photo is owned but not yet stored (storage_key NULL)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'p1', storage_key: null }] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get('/img/photo/p1');
    expect(res.status).toBe(404);
  });
});

describe('GET /img/crop/:id', () => {
  it('302 → signed URL when the crop is owned and stored', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'c1', crop_storage_key: 'u1/b1/crops/c1.webp' }] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get('/img/crop/c1');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe(
      'http://127.0.0.1:9000/notbulk/u1/b1/crops/c1.webp?sig=canned',
    );
  });

  it('404 when the crop is not owned', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get('/img/crop/c1');
    expect(res.status).toBe(404);
  });
});
