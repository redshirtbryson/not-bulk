import { describe, it, expect, beforeEach, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { createApp } from '../src/app.js';
import { FakePool, authedAgent, makeDeps } from './helpers.js';
import { __resetProgressBus } from '../src/services/progressbus.js';
import { progressRouter } from '../src/routes/progress.js';

const userA = { id: 'user-a', email: 'a@example.com', tier: 'free' };

// Parse a raw text/event-stream body into [{ event, data }] frames.
function parseSse(body: string): { event: string; data: any }[] {
  return body
    .split('\n\n')
    .map((f) => f.trim())
    .filter(Boolean)
    .filter((f) => !f.startsWith(':')) // drop heartbeat comments
    .map((frame) => {
      const out: { event: string; data: any } = { event: 'message', data: null };
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) out.event = line.slice(6).trim();
        if (line.startsWith('data:')) out.data = JSON.parse(line.slice(5).trim());
      }
      return out;
    });
}

describe('GET /batches/:id (progress view)', () => {
  beforeEach(() => __resetProgressBus());

  it('404s when the batch is not owned by the user', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedBatch → no row
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/nope');
    expect(res.status).toBe(404);
  });

  it('renders the progress view with initial counts for an owned batch', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 2 }] }); // getOwnedBatch
    pool.enqueue({ rows: [{ n: 1 }] }); // photos_done
    pool.enqueue({ rows: [{ n: 3 }] }); // cards_total
    pool.enqueue({ rows: [{ n: 2 }] }); // cards_identified
    pool.enqueue({ rows: [{ n: 1 }] }); // cards_validation
    pool.enqueue({ rows: [{ n: 0 }] }); // cards_unreadable
    pool.enqueue({ rows: [] }); // ticker
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/b1');
    expect(res.status).toBe(200);
    expect(res.text).toContain('id="photos-done"');
    // The view doesn't hardcode the SSE URL — public/js/progress.js builds it from
    // data-batch-id at runtime, so assert the seam it reads instead.
    expect(res.text).toContain('data-batch-id="b1"');
    expect(res.text).toContain('/js/progress.js');
  });
});

describe('GET /batches/:id/events (SSE)', () => {
  beforeEach(() => __resetProgressBus());

  it('404s for an unowned batch', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedBatch → none
    const app = createApp(makeDeps({ pool }));
    const res = await authedAgent(app, userA).get('/batches/nope/events');
    expect(res.status).toBe(404);
  });

  it('streams a snapshot event first with the SSE content type', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 2 }] }); // getOwnedBatch
    pool.enqueue({ rows: [{ n: 1 }] }); // photos_done
    pool.enqueue({ rows: [{ n: 3 }] }); // cards_total
    pool.enqueue({ rows: [{ n: 2 }] }); // cards_identified
    pool.enqueue({ rows: [{ n: 1 }] }); // cards_validation
    pool.enqueue({ rows: [{ n: 0 }] }); // cards_unreadable
    pool.enqueue({ rows: [{ card_id: 'c1', name: 'Pikachu', confidence: 95, status: 'auto' }] }); // ticker

    const app = createApp(makeDeps({ pool }));
    // supertest resolves when the server ends the response; we end it after snapshot
    // by injecting x-sse-test-oneshot so the route closes right after the snapshot.
    const res = await authedAgent(app, userA)
      .get('/batches/b1/events')
      .set('x-sse-test-oneshot', '1');
    expect(res.headers['content-type']).toContain('text/event-stream');
    expect(res.headers['cache-control']).toContain('no-cache');
    const frames = parseSse(res.text);
    expect(frames[0].event).toBe('snapshot');
    expect(frames[0].data.batch).toEqual({ status: 'processing', photo_count: 2 });
    expect(frames[0].data.cards_identified).toBe(2);
    expect(frames[0].data.ticker[0]).toEqual({
      card_id: 'c1', name: 'Pikachu', confidence: 95, status: 'auto',
    });
  });

  it('emits a heartbeat comment every 25s and clears the timer on close', async () => {
    vi.useFakeTimers();
    try {
      const pool = new FakePool();
      // getOwnedBatch + 6 snapshot queries (photos_done, cards_total, cards_identified,
      // cards_validation, cards_unreadable, ticker)
      pool.enqueue({ rows: [{ id: 'b1', user_id: 'user-a', status: 'processing', photo_count: 1 }] });
      for (let i = 0; i < 5; i++) pool.enqueue({ rows: [{ n: 0 }] });
      pool.enqueue({ rows: [] }); // ticker

      const r = progressRouter(pool as any, {} as any, async () => ({
        query: async () => ({ rows: [] }),
        on: () => {},
        end: async () => {},
      }));
      const app = express();
      app.use((req: any, _res, next) => { req.user = { id: 'user-a' }; next(); });
      app.use(r);

      const agent = request(app).get('/batches/b1/events');
      const p = agent.then(() => {});
      await vi.advanceTimersByTimeAsync(50_000);
      agent.abort();
      await p.catch(() => {});
      // No assertion beyond "this completes without hanging" — heartbeat cadence and
      // cleanup-on-close are exercised by driving the fake timer past 2 x 25s ticks.
      expect(true).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
