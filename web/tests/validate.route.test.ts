import { describe, it, expect } from 'vitest';
import { createApp } from '../src/app.js';
import { FakePool, anonAgent, authedAgent, makeDeps } from './helpers.js';

const userA = { id: 'user-a', email: 'a@example.com', tier: 'free' };

describe('GET /batches/:id/validate', () => {
  it('404s for an unowned batch', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedBatch → none
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/validate');
    expect(res.status).toBe(404);
  });

  it('renders the earliest validation card with top candidate + alternates (text only)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 1 }] }); // getOwnedBatch
    pool.enqueue({ rows: [{                                    // next card
      id: 'c1', card_ref_id: 'base1-4', finish: null, finish_needs_confirmation: true,
      confidence: 62, status: 'validation', crop_index: 0,
      candidates: [{ card_ref_id: 'base1-4', score: 0.62 }, { card_ref_id: 'base1-2', score: 0.5 }],
    }] });
    pool.enqueue({ rows: [                                     // candidate ref names
      { id: 'base1-4', name: 'Charizard', set_name: 'Base', number: '4', finishes: ['holo'] },
      { id: 'base1-2', name: 'Blastoise', set_name: 'Base', number: '2', finishes: ['holo'] },
    ] });
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/validate');
    expect(res.status).toBe(200);
    expect(res.text).toContain('/img/crop/c1');   // user's own crop as an image
    expect(res.text).toContain('Charizard');
    expect(res.text).toContain('Blastoise');
    expect(res.text).not.toContain('images.pokemontcg.io'); // Assembly Resolution 9: no external ref image
    expect(res.text).toContain('name="finish"'); // finish selector shown (needs_confirmation)
  });

  it('unauthenticated GET redirects 302 (page route, not /api/)', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await anonAgent(app).get('/batches/b1/validate');
    expect(res.status).toBe(302);
  });
});

describe('POST /cards/:id/validate', () => {
  it('marks validated when the chosen ref equals the original prediction and enqueues correction', async () => {
    const pool = new FakePool();
    const client = pool.client;
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: false }] }); // getOwnedCard
    // transaction: BEGIN, UPDATE card, enqueue insert, no merge match, COMMIT
    client.enqueue({ rows: [] });                       // BEGIN
    client.enqueue({ rows: [{ id: 'c1' }] });           // UPDATE cards ... RETURNING id
    client.enqueue({ rows: [] });                       // duplicate-merge lookup → none
    client.enqueue({ rows: [{ id: 'job-1' }] });        // enqueue ingest_correction
    client.enqueue({ rows: [] });                       // COMMIT
    pool.enqueue({ rows: [] });                          // post-commit NOTIFY jobs_wake

    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA)
      .post('/cards/c1/validate')
      .type('form').send({ card_ref_id: 'base1-4' });
    expect(res.status).toBe(302);
    const upd = client.calls.find((c) => c.sql.includes('UPDATE cards'))!;
    expect(upd.sql).toMatch(/status=\$/);
    expect(upd.params).toContain('validated');           // equals prediction → validated
    const enq = client.calls.find((c) => c.sql.includes('INSERT INTO jobs'))!;
    // enqueue() JSON.stringifies the payload before binding it as a param (see services/jobs.ts);
    // parse each string param and find the one shaped like the ingest_correction payload.
    const payload = enq.params
      .map((p: any) => { try { return JSON.parse(p); } catch { return null; } })
      .find((p: any) => p && typeof p === 'object' && 'actual_ref_id' in p);
    expect(payload).toEqual({ card_id: 'c1', actual_ref_id: 'base1-4', predicted_ref_id: 'base1-4' });
  });

  it('marks corrected when the chosen ref differs from the prediction', async () => {
    const pool = new FakePool();
    const client = pool.client;
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: false }] });
    client.enqueue({ rows: [] });                       // BEGIN
    client.enqueue({ rows: [{ id: 'c1' }] });           // UPDATE
    client.enqueue({ rows: [] });                       // merge lookup → none
    client.enqueue({ rows: [{ id: 'job-1' }] });        // enqueue
    client.enqueue({ rows: [] });                       // COMMIT
    pool.enqueue({ rows: [] });                          // NOTIFY

    const app = createApp(makeDeps({ pool }));
    await authedAgent(app, userA).post('/cards/c1/validate').type('form').send({ card_ref_id: 'base1-2' });
    const upd = client.calls.find((c) => c.sql.includes('UPDATE cards'))!;
    expect(upd.params).toContain('corrected');
    const enq = client.calls.find((c) => c.sql.includes('INSERT INTO jobs'))!;
    const payload = enq.params
      .map((p: any) => { try { return JSON.parse(p); } catch { return null; } })
      .find((p: any) => p && typeof p === 'object' && 'actual_ref_id' in p);
    expect(payload).toEqual({ card_id: 'c1', actual_ref_id: 'base1-2', predicted_ref_id: 'base1-4' });
  });

  it('sets finish and clears finish_needs_confirmation when finish is provided', async () => {
    const pool = new FakePool();
    const client = pool.client;
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: true }] });
    client.enqueue({ rows: [] });                       // BEGIN
    client.enqueue({ rows: [{ id: 'c1' }] });           // UPDATE
    client.enqueue({ rows: [] });                       // merge lookup → none
    client.enqueue({ rows: [{ id: 'job-1' }] });        // enqueue
    client.enqueue({ rows: [] });                       // COMMIT
    pool.enqueue({ rows: [] });                          // NOTIFY

    const app = createApp(makeDeps({ pool }));
    await authedAgent(app, userA).post('/cards/c1/validate').type('form')
      .send({ card_ref_id: 'base1-4', finish: 'holo' });
    const upd = client.calls.find((c) => c.sql.includes('UPDATE cards'))!;
    expect(upd.sql).toContain('finish=');
    expect(upd.sql).toContain('finish_needs_confirmation=false');
    expect(upd.params).toContain('holo');
  });

  it('merges into the earliest same (ref, finish) card: target quantity++ and this card status=merged', async () => {
    const pool = new FakePool();
    const client = pool.client;
    pool.enqueue({ rows: [{ id: 'c2', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: false }] });
    client.enqueue({ rows: [] });                       // BEGIN
    client.enqueue({ rows: [{ id: 'c2' }] });           // UPDATE (initial validated/corrected)
    client.enqueue({ rows: [{ id: 'c1' }] });           // merge lookup → earliest match c1
    client.enqueue({ rows: [] });                       // UPDATE target quantity++
    client.enqueue({ rows: [] });                       // UPDATE this card status='merged'
    client.enqueue({ rows: [{ id: 'job-1' }] });        // enqueue correction (still records the ID)
    client.enqueue({ rows: [] });                       // COMMIT
    pool.enqueue({ rows: [] });                          // NOTIFY

    const app = createApp(makeDeps({ pool }));
    await authedAgent(app, userA).post('/cards/c2/validate').type('form')
      .send({ card_ref_id: 'base1-4', finish: 'holo' });
    const inc = client.calls.find((c) => c.sql.includes('quantity=quantity+1'))!;
    expect(inc.params).toContain('c1');                 // target is the earliest match
    const merged = client.calls.find((c) => c.sql.includes("'merged'") || c.params?.includes('merged'))!;
    expect(merged).toBeTruthy();
  });

  it('rejects an invalid finish enum with 400', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: true }] });
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).post('/cards/c1/validate').type('form')
      .send({ card_ref_id: 'base1-4', finish: 'sparkly' });
    expect(res.status).toBe(400);
  });
});

describe('POST /cards/:id/skip and /cards/:id/not-card', () => {
  it('skip sets status=skipped and redirects to the validate view', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: false }] }); // getOwnedCard
    pool.enqueue({ rows: [] }); // UPDATE
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).post('/cards/c1/skip');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/batches/b1/validate');
    const upd = pool.calls.find((c) => c.sql.includes('UPDATE cards'))!;
    expect(upd.params).toContain('skipped');
  });

  it('not-card sets status=not_card and redirects to the validate view', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'c1', batch_id: 'b1', card_ref_id: 'base1-4', finish_needs_confirmation: false }] }); // getOwnedCard
    pool.enqueue({ rows: [] }); // UPDATE
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).post('/cards/c1/not-card');
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe('/batches/b1/validate');
    const upd = pool.calls.find((c) => c.sql.includes('UPDATE cards'))!;
    expect(upd.params).toContain('not_card');
  });

  it('404s skip for an unowned card', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedCard → none
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).post('/cards/c1/skip');
    expect(res.status).toBe(404);
  });
});
