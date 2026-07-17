import { describe, it, expect } from 'vitest';
import { checkAndReserve } from '../src/services/quotas.js';
import { FakePool } from './helpers.js';
import type { Config } from '../src/config.js';

const cfg = {
  quotas: { batches_per_day: 5, photos_per_day: 50, fetches_per_day: 20 },
} as unknown as Config;

describe('checkAndReserve', () => {
  it('reserves a single-batch/multi-photo request and passes exact SQL params', async () => {
    // The conditional upsert RETURNs a row when within caps.
    const pool = new FakePool();
    pool.enqueue({ rows: [{ user_id: 'u1' }] });
    const res = await checkAndReserve(pool as any, cfg, 'u1', { batches: 1, photos: 3 });
    expect(res).toEqual({ ok: true });

    const call = pool.calls[0];
    // Params: [userId, wantBatches, wantPhotos, wantFetches, capBatches, capPhotos, capFetches]
    expect(call.params).toEqual(['u1', 1, 3, 0, 5, 50, 20]);
    expect(call.sql).toMatch(/INSERT INTO usage/i);
    expect(call.sql).toMatch(/ON CONFLICT \(user_id, day\) DO UPDATE/i);
    expect(call.sql).toMatch(/day = CURRENT_DATE/i);
    expect(call.sql).toMatch(/RETURNING/i);
  });

  it('returns ok:false naming the exceeded dimension when no row is returned', async () => {
    // Empty result = the WHERE guard failed = a cap would be exceeded.
    // The service re-queries current counts to name the offending dimension.
    const pool = new FakePool();
    pool.enqueue({ rows: [] });                                   // upsert: no row (blocked)
    pool.enqueue({ rows: [{ batches: 5, photos: 10, fetches: 0 }] }); // re-query current counts
    const res = await checkAndReserve(pool as any, cfg, 'u1', { batches: 1, photos: 3 });
    expect(res.ok).toBe(false);
    expect(res.reason).toBe('batches'); // 5 + 1 > 5 cap
  });

  it('names photos when the photo dimension is the one over cap', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    pool.enqueue({ rows: [{ batches: 0, photos: 49, fetches: 0 }] });
    const res = await checkAndReserve(pool as any, cfg, 'u1', { batches: 1, photos: 3 });
    expect(res.ok).toBe(false);
    expect(res.reason).toBe('photos'); // 49 + 3 > 50 cap
  });

  it('reserves fetches alongside batches+photos (URL path shape)', async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ user_id: 'u1' }] });
    const res = await checkAndReserve(pool as any, cfg, 'u1', { batches: 1, photos: 4, fetches: 4 });
    expect(res).toEqual({ ok: true });
    expect(pool.calls[0].params).toEqual(['u1', 1, 4, 4, 5, 50, 20]);
  });
});
