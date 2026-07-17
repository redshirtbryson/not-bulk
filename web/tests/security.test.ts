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

  it('every owned POST returns 404 for a stranger AND is byte-identical to a missing id', async () => {
    const posts = [
      (id: string) => `/cards/${id}/validate`,
      (id: string) => `/cards/${id}/skip`,
      (id: string) => `/cards/${id}/not-card`,
    ];
    for (const make of posts) {
      // Case 1: the id belongs to user A (exists, but not owned by B).
      const p1 = new FakePool();
      p1.enqueue({ rows: [] }); // getOwnedCard → none for B
      const app1 = createApp(makeDeps({ pool: p1 }));
      const owned = await authedAgent(app1, userB)
        .post(make('card-owned-by-A')).type('form').send({ card_ref_id: 'base1-4' });

      // Case 2: a genuinely non-existent id.
      const p2 = new FakePool();
      p2.enqueue({ rows: [] }); // getOwnedCard → none, id doesn't exist at all
      const app2 = createApp(makeDeps({ pool: p2 }));
      const missing = await authedAgent(app2, userB)
        .post(make('this-id-does-not-exist')).type('form').send({ card_ref_id: 'base1-4' });

      expect(owned.status).toBe(404);
      expect(missing.status).toBe(404);
      // No existence oracle: same status AND same body.
      expect(owned.text).toBe(missing.text);
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

describe('Session fixation: a token is rejected after logout', () => {
  // src/app.ts now mounts authRoutes() from src/auth/routes.ts at the app level, so
  // POST /auth/logout is reachable through createApp. This exercises the real
  // session-fixation scenario end-to-end: a valid session authenticates a page route,
  // POST /auth/logout calls destroySession() (DELETE ... WHERE token_hash = $1), and
  // the SAME pre-logout cookie token must then be rejected -- proving logout actually
  // invalidates the session server-side rather than merely clearing the client cookie.

  it('POST /auth/logout is mounted (302, not 404) and destroys the session', async () => {
    const pool = new FakePool();
    const app = createApp(
      makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }),
    );

    // Pre-logout: the token authenticates (control, proves the token was ever valid).
    // FakePool is a strict FIFO queue shared by sessionMiddleware + the route, so rows
    // must be enqueued in the exact order the request issues queries: lookupSession,
    // touchSession, then getOwnedBatch + snapshot()'s 6 queries.
    const { token } = makeSession(pool, userA); // lookupSession's windowed SELECT
    pool.enqueue({ rows: [] }); // touchSession no-op
    pool.enqueue({ rows: [{ id: 'b1', user_id: userA.id, status: 'processing', photo_count: 1, origin_url: null, created_at: new Date().toISOString() }] }); // getOwnedBatch
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() photos_done
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_total
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_identified
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_validation
    pool.enqueue({ rows: [{ n: 0 }] }); // snapshot() cards_unreadable
    pool.enqueue({ rows: [] }); // snapshot() ticker
    const before = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(before.status).toBe(200);

    // Logout: authRoutes is mounted, so this reaches destroySession() and 302s -- not 404.
    pool.enqueue({ rows: [] }); // destroySession's DELETE
    const logout = await anonAgent(app).post('/auth/logout').set('Cookie', `nb_session=${token}`);
    expect(logout.status).toBe(302);

    // Post-logout: lookupSession now finds no row for the same token (session row deleted).
    pool.enqueue({ rows: [] });
    const after = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(after.status).toBe(302);
    expect(after.headers.location).toBe('/');
  });
});

describe('Session lifecycle', () => {
  // `lookupSession` issues exactly ONE query per request: a single windowed SELECT
  // that ANDs together the absolute-expiry, idle-timeout, and active-user predicates
  // (see src/auth/sessions.ts). From the HTTP layer, "expired", "idle-timed-out", and
  // "row deleted by logout" are indistinguishable: all three produce a 0-row result
  // from that one query, and requireUser() reacts identically (302 to /) regardless of
  // WHICH predicate excluded the row. A fake pool that just enqueues {rows: []} cannot
  // tell these cases apart, so three separately-named tests asserting the same
  // enqueue-then-302 shape would overstate coverage -- they don't independently
  // exercise the three SQL predicates, only the single "no row → reject" branch.
  // (The predicates themselves are covered by static presence in sessions.ts, not
  // behaviorally here.) Collapsed to one honestly-named test; the positive-control
  // "valid session reaches the route" test below stays separate and intact.

  it('rejects when lookupSession finds no valid session (expired / idle / destroyed all produce this)', async () => {
    const pool = new FakePool();
    const token = randomBytes(32).toString('base64url');
    pool.enqueue({ rows: [] }); // windowed SELECT returns no row, regardless of which predicate excluded it
    const app = createApp(makeDeps({ pool, sessionMiddleware: sessionMiddleware(pool as any, testCfg) }));
    const res = await anonAgent(app).get('/batches/b1').set('Cookie', `nb_session=${token}`);
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/');
  });

  it('a valid, non-expired session reaches the route (control: proves the 302 above is a real rejection, not a broken app)', async () => {
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
