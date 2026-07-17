// Process-wide singleton owning ONE dedicated pg client that LISTENs batch_progress
// and fans out parsed NOTIFY payloads to per-batch subscriber sets.

export type NotifyPayload = {
  batch_id: string;
  event: 'photo_stored' | 'photo_done' | 'card_identified' | 'batch_complete';
  photo_id?: string;
  card_id?: string;
};

export interface PgLikeClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: any[] }>;
  on(event: 'notification' | 'error', cb: (arg: any) => void): void;
  end(): Promise<void>;
}

type Subscriber = (evt: NotifyPayload) => void;

export class ProgressBus {
  private subs = new Map<string, Set<Subscriber>>();
  private client: PgLikeClient | null = null;
  private starting: Promise<void> | null = null;
  private backoffMs = 250;
  private readonly maxBackoffMs = 10_000;

  constructor(private clientFactory: () => Promise<PgLikeClient>) {}

  subscribe(batchId: string, fn: Subscriber): () => void {
    let set = this.subs.get(batchId);
    if (!set) { set = new Set(); this.subs.set(batchId, set); }
    set.add(fn);
    // Lazy init: connect + LISTEN on the first subscriber overall.
    void this.ensureListening();
    return () => {
      const s = this.subs.get(batchId);
      if (!s) return;
      s.delete(fn);
      if (s.size === 0) this.subs.delete(batchId);
    };
  }

  private async ensureListening(): Promise<void> {
    if (this.client || this.starting) return this.starting ?? undefined;
    this.starting = this.connect();
    try { await this.starting; } finally { this.starting = null; }
  }

  private async connect(): Promise<void> {
    try {
      const client = await this.clientFactory();
      client.on('notification', (msg: { channel: string; payload?: string }) => {
        if (msg.channel !== 'batch_progress' || !msg.payload) return;
        let evt: NotifyPayload;
        try { evt = JSON.parse(msg.payload); } catch { return; }
        const set = this.subs.get(evt.batch_id);
        if (!set) return;
        for (const fn of Array.from(set)) {
          try { fn(evt); } catch { /* one subscriber must not break others */ }
        }
      });
      client.on('error', (err: Error) => { void this.reconnect(err); });
      await client.query('LISTEN batch_progress');
      this.client = client;
      this.backoffMs = 250; // reset on a clean connect
    } catch (err) {
      await this.reconnect(err as Error);
    }
  }

  private async reconnect(_err: Error): Promise<void> {
    const dead = this.client;
    this.client = null;
    if (dead) { try { await dead.end(); } catch { /* already gone */ } }
    // Nothing to relisten to if every subscriber has left.
    if (this.subs.size === 0) return;
    const wait = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
    await new Promise((r) => setTimeout(r, wait));
    await this.connect(); // re-connect + re-LISTEN
  }
}

let singleton: ProgressBus | null = null;
export function getProgressBus(clientFactory: () => Promise<PgLikeClient>): ProgressBus {
  if (!singleton) singleton = new ProgressBus(clientFactory);
  return singleton;
}
// Test-only reset so suites don't leak the module-level singleton.
export function __resetProgressBus(): void { singleton = null; }
