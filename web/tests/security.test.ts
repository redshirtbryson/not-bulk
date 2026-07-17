import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createApp } from '../src/app.js';
import { sessionMiddleware } from '../src/middleware/session.js';
import {
  FakePool,
  authedAgent,
  anonAgent,
  makeDeps,
  makeSession,
  expireSession,
  idleSession,
  testCfg,
} from './helpers.js';

const userA = { id: 'user-a', email: 'a@example.com', tier: 'free' };
const userB = { id: 'user-b', email: 'b@example.com', tier: 'free' };

// The owned GET routes and the id they take.
const ownedGets = [
  (id: string) => `/batches/${id}`,
  (id: string) => `/batches/${id}/events`,
  (id: string) => `/batches/${id}/validate`,
  (id: string) => `/img/photo/${id}`,
  (id: string) => `/img/crop/${id}`,
];

describe('IDOR: user B cannot reach user A resources (no existence oracle)', () => {
  it('every owned GET returns 404 for a stranger AND is byte-identical to a missing id', async () => {
    for (const make of ownedGets) {
      // Case 1: the id belongs to user A (exists, but not owned by B).
      const p1 = new FakePool();
      p1.enqueue({ rows: [] }); // owned-lookup filtered by user_id → no row for B
      const app1 = createApp(makeDeps({ pool: p1 }));
      const owned = await authedAgent(app1, userB).get(make('real-id-owned-by-A'));

      // Case 2: a genuinely non-existent id.
      const p2 = new FakePool();
      p2.enqueue({ rows: [] });
      const app2 = createApp(makeDeps({ pool: p2 }));
      const missing = await authedAgent(app2, userB).get(make('this-id-does-not-exist'));

      expect(owned.status).toBe(404);
      expect(missing.status).toBe(404);
      // No existence oracle: same status AND same body.
      expect(owned.text).toBe(missing.text);
    }
  });

  it('every owned POST returns 404 for a stranger', async () => {
    const posts = [
      (id: string) => `/cards/${id}/validate`,
      (id: string) => `/cards/${id}/skip`,
      (id: string) => `/cards/${id}/not-card`,
    ];
    for (const make of posts) {
      const pool = new FakePool();
      pool.enqueue({ rows: [] }); // getOwnedCard → none for B
      const app = createApp(makeDeps({ pool }));
      const res = await authedAgent(app, userB)
        .post(make('card-owned-by-A')).type('form').send({ card_ref_id: 'base1-4' });
      expect(res.status).toBe(404);
    }
  });
});

describe('Unauthenticated access', () => {
  it('page routes 302 to / ; /api/* routes return 401 JSON', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));

    const page = await anonAgent(app).get('/batches/b1/validate');
    expect(page.status).toBe(302);
    expect(page.headers.location).toBe('/');

    const api = await anonAgent(app).get('/api/search-refs?q=char');
    expect(api.status).toBe(401);
    expect(api.body).toEqual({ error: 'unauthorized' });
  });
});

describe('KNOWN GAP: /auth/* routes are not reachable through createApp', () => {
  // src/app.ts never imports or mounts authRoutes() from src/auth/routes.ts (grep
  // confirms no `authRoutes` reference anywhere in app.ts). Task 3's own report
  // ("m2-task-3-report.md") explicitly deferred this wiring to "later tasks" and no
  // task since has picked it up. This means POST /auth/logout, GET /auth/verify, and
  // POST /auth/magic-link are all unreachable via HTTP in the deployed app today --
  // magic-link login and logout do not work end-to-end, and this suite CANNOT exercise
  // the brief's session-fixation scenario ("a pre-logout token is rejected after
  // logout") through the real app, because there is no HTTP logout endpoint to call.
  //
  // This is flagged as a finding in m2-task-16-report.md, not silently routed around.
  // It is NOT an ownership/IDOR bug (no cross-user data exposure) -- it is a wiring
  // omission that breaks auth entirely. Fixing app.ts is out of scope for this
  // test-only task; this test pins the gap so it fails loudly (rather than silently
  // 404ing forever) once someone tries to wire it, and must be deleted/replaced by a
  // real fixation test in the same change that adds the wiring.
  it('POST /auth/logout 404s today because authRoutes is not mounted in createApp', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await anonAgent(app).post('/auth/logout');
    expect(res.status).toBe(404); // <- flip to 302 once app.ts mounts authRoutes()
  });
});

describe('Session lifecycle', () => {
  // NOTE: `lookupSession` issues exactly ONE query per request (the windowed SELECT);
  // `makeSession`'s own enqueue is for the "valid session" case only, so the
  // expired/idle/destroyed cases below mint a bare token and enqueue just the single
  // 0-row lookup result directly (calling makeSession first would enqueue an extra,
  // unconsumed row and mask the real behavior under test).

  it('an expired (absolute) session is rejected', async () => {
    const pool = new FakePool();
    const token = randomBytes(32).toString('base64url');
    expireSession(pool, token); // canned row has expires_at in the past (0-row lookup)
    const app = createApp(makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }));
    const res = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
  });

  it('an idle-timed-out session is rejected', async () => {
    const pool = new FakePool();
    const token = randomBytes(32).toString('base64url');
    idleSession(pool, token); // last_seen_at older than idle window (0-row lookup)
    const app = createApp(makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }));
    const res = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
  });

  it('a token whose session row is gone (e.g. destroyed by logout) is rejected', async () => {
    // Exercises the same "dead token" path a post-logout fixation attempt hits:
    // lookupSession finds no row, so requireUser() must redirect, not authenticate.
    const pool = new FakePool();
    const token = randomBytes(32).toString('base64url');
    pool.enqueue({ rows: [] }); // session row now gone (deleted) → lookup returns nothing
    const app = createApp(makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }));
    const after = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(after.status).toBe(302);
    expect(after.headers.location).toBe('/');
  });

  it('a valid, non-expired session reaches the route (control: proves the 302s above are real rejections, not a broken app)', async () => {
    const pool = new FakePool();
    const { token } = makeSession(pool, userA); // one valid-session row
    pool.enqueue({ rows: [{ id: 'b1', user_id: userA.id, status: 'processing', photo_count: 1, origin_url: null, created_at: new Date().toISOString() }] }); // getOwnedBatch
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() photos_done
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_total
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_identified
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_validation
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_unreadable
    pool.enqueue({ rows: [] }); // snapshot() ticker
    const app = createApp(makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }));
    const res = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(res.status).toBe(200);
  });
});

describe('Quota abuse', () => {
  it('the 6th batch of the day is rejected with 400', async () => {
    const pool = new FakePool();
    // The route runs everything through client.connect() -> BEGIN, checkAndReserve's
    // upsert, (blocked) re-query, ROLLBACK -- all drawn from the same shared FakeClient
    // queue in call order.
    pool.enqueue({ rows: [] }); // BEGIN
    pool.enqueue({ rows: [] }); // INSERT ... ON CONFLICT ... WHERE guard → blocked, no row
    pool.enqueue({ rows: [{ batches: 5, photos: 0, fetches: 0 }] }); // re-query current counts
    pool.enqueue({ rows: [] }); // ROLLBACK
    const app = createApp(makeDeps({ pool, verifyTurnstile: async () => true }));
    const res = await authedAgent(app, userA)
      .post('/batches')
      .field('cf-turnstile-response', 'x')
      .field('urls', 'https://i.imgur.com/abc.jpg'); // URL path avoids multipart file setup
    expect(res.status).toBe(400);
    expect(res.text).toMatch(/quota exceeded/i);
    expect(res.text).toMatch(/batches/i);
  });
});

describe('Architectural tripwire: every owned-resource queries/*.ts export is user-scoped', () => {
  // users.ts is deliberately excluded: it is the identity table itself (looked up by
  // its own id / upserted by email to establish identity), not an owned resource
  // reached through a batch/photo/card ownership chain. Every OTHER file in
  // src/queries/ backs a route from the "owned GET/POST" matrix above and must filter
  // by userId so no id-guessing route can read another user's rows.
  const EXEMPT = new Set(['users.ts']);

  it('every exported async function in queries/*.ts (excluding identity lookups) takes a userId: string arg', () => {
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const dir = path.resolve(__dirname, '../src/queries');
    const files = fs.readdirSync(dir).filter((f) => f.endsWith('.ts') && !EXEMPT.has(f));
    expect(files.length).toBeGreaterThan(0);

    // Match exported functions; each signature (up to the first `)`) must name userId.
    const exportFn = /export\s+(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)/g;
    const offenders: string[] = [];
    for (const file of files) {
      const src = fs.readFileSync(path.join(dir, file), 'utf8');
      let m: RegExpExecArray | null;
      while ((m = exportFn.exec(src)) !== null) {
        const [, name, sig] = m;
        if (!/userId\s*:\s*string/.test(sig)) offenders.push(`${file}:${name}`);
      }
    }
    expect(offenders, `owned-query helpers missing userId: ${offenders.join(', ')}`).toEqual([]);
  });

  it('users.ts exists and is exempt only because it has no userId-filtered lookups to bypass', () => {
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const dir = path.resolve(__dirname, '../src/queries');
    const src = fs.readFileSync(path.join(dir, 'users.ts'), 'utf8');
    // Guard the exemption itself: if someone later adds a userId-taking export to
    // users.ts, fine; but it must never expose an unscoped lookup keyed by anything
    // OTHER than the row's own id/email (i.e. no batchId/cardId/photoId params sneaking
    // in without a userId filter).
    expect(src).not.toMatch(/\b(batchId|cardId|photoId)\s*:/);
  });
});
