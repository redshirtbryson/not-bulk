import { describe, it, expect } from 'vitest';
import { createApp } from '../src/app.js';
import { FakePool, anonAgent, authedAgent, makeDeps } from './helpers.js';

const userA = { id: 'user-a', email: 'a@example.com', tier: 'free' };

describe('GET /api/search-refs', () => {
  it('matches name prefix (ILIKE) OR exact number, limit 10, JSON, params bound', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [
      { id: 'base1-4', name: 'Charizard', set_name: 'Base', number: '4' },
      { id: 'base1-11', name: 'Charmander', set_name: 'Base', number: '46' },
    ] });
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/api/search-refs?q=char');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toContain('application/json');
    expect(res.body).toHaveLength(2);
    expect(res.body[0]).toEqual({ id: 'base1-4', name: 'Charizard', set_name: 'Base', number: '4' });

    const call = pool.calls[0];
    expect(call.sql).toMatch(/lower\(name\)\s+LIKE/i);
    expect(call.sql).toMatch(/number\s*=/i);
    expect(call.sql).toMatch(/LIMIT 10/i);
    // Injection safety: user input goes through a bound param, never interpolated.
    expect(call.params).toContain('char%');
    expect(call.sql).not.toContain('char');
  });

  it('returns [] for an empty query without hitting the DB', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/api/search-refs?q=');
    expect(res.status).toBe(200);
    expect(res.body).toEqual([]);
    expect(pool.calls).toHaveLength(0);
  });

  it('unauthenticated GET returns 401 JSON (not a redirect, per /api/ convention)', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await anonAgent(app).get('/api/search-refs?q=char');
    expect(res.status).toBe(401);
    expect(res.headers['content-type']).toContain('application/json');
  });
});
