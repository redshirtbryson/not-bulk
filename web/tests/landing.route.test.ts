import { describe, it, expect } from 'vitest';
import { createApp } from '../src/app.js';
import { FakePool, authedAgent, anonAgent, makeDeps } from './helpers.js';

const DISCLAIMER =
  'NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by ' +
  'Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.';

describe('GET / (landing)', () => {
  it('anon: shows wordmark, tagline, sign-in form, disclaimer — and NO upload form', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await anonAgent(app).get('/');
    expect(res.status).toBe(200);
    expect(res.text).toContain('NotBulk');
    expect(res.text).toContain("Find out what's not bulk");
    expect(res.text).toContain('action="/auth/magic-link"');
    expect(res.text).toContain(DISCLAIMER);
    expect(res.text).not.toContain('action="/batches"'); // upload hidden when anon
  });

  it('authed: shows the upload + URL paste form posting to /batches', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, { id: 'u1', email: 'a@x.com', tier: 'free' }).get('/');
    expect(res.status).toBe(200);
    expect(res.text).toContain('action="/batches"');
    expect(res.text).toContain('type="file"');
    expect(res.text).toContain('name="urls"');
  });
});
