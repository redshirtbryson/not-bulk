import { describe, it, expect } from 'vitest';
import { enqueue } from '../src/services/jobs.js';
import { FakePool } from './helpers.js';

describe('enqueue', () => {
  it('inserts a jobs row with type, payload, batch_id, user_id and returns the id', async () => {
    // FakePool doubles as a PoolClient here (has .query).
    const client = new FakePool();
    client.enqueue({ rows: [{ id: 'job-1' }] });
    const id = await enqueue(client as any, {
      type: 'detect',
      payload: { photo_id: 'p1' },
      batchId: 'b1',
      userId: 'u1',
    });
    expect(id).toBe('job-1');
    const call = client.calls[0];
    expect(call.sql).toMatch(/INSERT INTO jobs/i);
    // params: [id, type, payloadJson, batchId, userId]
    expect(call.params[1]).toBe('detect');
    expect(JSON.parse(call.params[2])).toEqual({ photo_id: 'p1' });
    expect(call.params[3]).toBe('b1');
    expect(call.params[4]).toBe('u1');
  });

  it('rejects a payload that fails its zod schema (detect requires photo_id)', async () => {
    const client = new FakePool();
    await expect(
      enqueue(client as any, { type: 'detect', payload: { wrong: 'x' } as any }),
    ).rejects.toThrow();
    expect(client.calls.length).toBe(0); // never touched the DB
  });

  it('validates ingest_correction payload shape (card_id + actual_ref_id + predicted_ref_id)', async () => {
    const client = new FakePool();
    client.enqueue({ rows: [{ id: 'job-2' }] });
    const id = await enqueue(client as any, {
      type: 'ingest_correction',
      payload: { card_id: 'c1', actual_ref_id: 'swsh1-25', predicted_ref_id: 'swsh1-99' },
    });
    expect(id).toBe('job-2');
  });

  it('accepts a null predicted_ref_id (no prior prediction)', async () => {
    const client = new FakePool();
    client.enqueue({ rows: [{ id: 'job-3' }] });
    const id = await enqueue(client as any, {
      type: 'ingest_correction',
      payload: { card_id: 'c1', actual_ref_id: 'swsh1-25', predicted_ref_id: null },
    });
    expect(id).toBe('job-3');
  });
});
