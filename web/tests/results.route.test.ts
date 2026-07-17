import { describe, it, expect } from 'vitest';
import { createApp } from '../src/app.js';
import { FakePool, authedAgent, makeDeps } from './helpers.js';

const userA = { id: 'user-a', email: 'a@example.com', tier: 'free' };

describe('GET /batches/:id/results', () => {
  it('404s for an unowned batch', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedBatch → none
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/results');
    expect(res.status).toBe(404);
  });

  it('renders a grid row per resolved card (auto/validated/corrected only)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'complete', photo_count: 2 }] }); // getOwnedBatch
    pool.enqueue({ rows: [                                    // resolved cards
      { id: 'c1', name: 'Charizard', set_name: 'Base', number: '4', finish: 'holofoil',
        confidence: 96, quantity: 1, status: 'auto' },
      { id: 'c2', name: 'Blastoise', set_name: 'Base', number: '2', finish: 'holofoil',
        confidence: 88, quantity: 2, status: 'validated' },
    ] });
    pool.enqueue({ rows: [{ n: 0 }] }); // remaining validation count
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1/results');
    expect(res.status).toBe(200);
    expect(res.text).toContain('/img/crop/c1');
    expect(res.text).toContain('Charizard');
    expect(res.text).toContain('/img/crop/c2');
    expect(res.text).toContain('Blastoise');
    expect(res.text).toContain('×2'); // quantity render
  });
});
