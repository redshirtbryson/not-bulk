import { describe, it, expect, vi } from 'vitest';
import { ProgressBus } from '../src/services/progressbus.js';
import type { PgLikeClient, NotifyPayload } from '../src/services/progressbus.js';

// A fake pg client we can drive: capture handlers, emit notifications on demand.
function makeFakeClient() {
  const handlers: Record<string, (arg: any) => void> = {};
  const queries: string[] = [];
  const client: PgLikeClient = {
    async query(sql: string) { queries.push(sql); return { rows: [] }; },
    on(event, cb) { handlers[event] = cb as any; },
    async end() {},
  };
  return {
    client,
    queries,
    emit(payload: NotifyPayload) {
      handlers['notification']?.({ channel: 'batch_progress', payload: JSON.stringify(payload) });
    },
    fail(err: Error) { handlers['error']?.(err); },
  };
}

describe('ProgressBus', () => {
  it('lazily LISTENs only when the first subscriber attaches', async () => {
    const fake = makeFakeClient();
    const bus = new ProgressBus(async () => fake.client);
    expect(fake.queries).toEqual([]); // no client work before a subscriber
    const unsub = bus.subscribe('batch-1', () => {});
    await vi.waitFor(() => expect(fake.queries).toContain('LISTEN batch_progress'));
    unsub();
  });

  it('fans out only to subscribers of the matching batch (isolation)', async () => {
    const fake = makeFakeClient();
    const bus = new ProgressBus(async () => fake.client);
    const a: NotifyPayload[] = [];
    const b: NotifyPayload[] = [];
    bus.subscribe('batch-A', (e) => a.push(e));
    bus.subscribe('batch-B', (e) => b.push(e));
    await vi.waitFor(() => expect(fake.queries).toContain('LISTEN batch_progress'));

    fake.emit({ batch_id: 'batch-A', event: 'photo_stored', photo_id: 'p1' });
    fake.emit({ batch_id: 'batch-B', event: 'card_identified', card_id: 'c9' });

    expect(a).toEqual([{ batch_id: 'batch-A', event: 'photo_stored', photo_id: 'p1' }]);
    expect(b).toEqual([{ batch_id: 'batch-B', event: 'card_identified', card_id: 'c9' }]);
  });

  it('unsubscribe stops delivery and does not affect other subscribers', async () => {
    const fake = makeFakeClient();
    const bus = new ProgressBus(async () => fake.client);
    const seen: string[] = [];
    const unsub = bus.subscribe('batch-A', () => seen.push('first'));
    bus.subscribe('batch-A', () => seen.push('second'));
    await vi.waitFor(() => expect(fake.queries).toContain('LISTEN batch_progress'));
    unsub();
    fake.emit({ batch_id: 'batch-A', event: 'photo_done', photo_id: 'p2' });
    expect(seen).toEqual(['second']);
  });
});
