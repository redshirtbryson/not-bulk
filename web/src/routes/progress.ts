import { Router, type Response } from 'express';
import type { Pool } from 'pg';
import type { Config } from '../config.js';
import type { AuthedRequest } from '../middleware/session.js';
import { requireUser } from '../middleware/session.js';
import { getOwnedBatch } from '../queries/batches.js';
import { getProgressBus, type NotifyPayload, type PgLikeClient } from '../services/progressbus.js';

const num = (r: { rows: any[] }) => Number(r.rows[0]?.n ?? 0);

async function snapshot(pool: Pool, batch: { id: string; status: string; photo_count: number }) {
  const [done, total, ident, valid, unread, ticker] = await Promise.all([
    pool.query(`SELECT count(*)::int AS n FROM photos WHERE batch_id=$1 AND status='done'`, [batch.id]),
    pool.query(
      `SELECT count(*)::int AS n FROM cards c JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`, [batch.id]),
    pool.query(
      `SELECT count(*)::int AS n FROM cards c JOIN photos p ON p.id=c.photo_id
         WHERE p.batch_id=$1 AND c.status IN ('auto','validated','corrected')`, [batch.id]),
    pool.query(
      `SELECT count(*)::int AS n FROM cards c JOIN photos p ON p.id=c.photo_id
         WHERE p.batch_id=$1 AND c.status='validation'`, [batch.id]),
    pool.query(
      `SELECT count(*)::int AS n FROM cards c JOIN photos p ON p.id=c.photo_id
         WHERE p.batch_id=$1 AND c.status='unreadable'`, [batch.id]),
    pool.query(
      `SELECT c.id AS card_id, r.name, c.confidence, c.status
         FROM cards c JOIN photos p ON p.id=c.photo_id
         LEFT JOIN card_refs r ON r.id=c.card_ref_id
         WHERE p.batch_id=$1 ORDER BY c.updated_at DESC LIMIT 20`, [batch.id]),
  ]);
  return {
    batch: { status: batch.status, photo_count: batch.photo_count },
    photos_done: num(done),
    cards_total: num(total),
    cards_identified: num(ident),
    cards_validation: num(valid),
    cards_unreadable: num(unread),
    ticker: ticker.rows,
  };
}

async function cardRow(pool: Pool, cardId: string) {
  const r = await pool.query(
    `SELECT c.id AS card_id, r.name, c.confidence, c.status
       FROM cards c LEFT JOIN card_refs r ON r.id=c.card_ref_id WHERE c.id=$1`, [cardId]);
  return r.rows[0] ?? null;
}

function writeEvent(res: Response, event: string, data: unknown) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

export function progressRouter(pool: Pool, cfg: Config, clientFactory: () => Promise<PgLikeClient>): Router {
  const r = Router();

  r.get('/batches/:id', requireUser(), async (req: AuthedRequest, res) => {
    const batch = await getOwnedBatch(pool, req.user!.id, req.params.id as string);
    if (!batch) return res.sendStatus(404);
    const snap = await snapshot(pool, batch);
    res.render('progress.njk', { batch, snap });
  });

  r.get('/batches/:id/events', requireUser(), async (req: AuthedRequest, res) => {
    const batch = await getOwnedBatch(pool, req.user!.id, req.params.id as string);
    if (!batch) return res.sendStatus(404);

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    if (typeof (res as any).flushHeaders === 'function') (res as any).flushHeaders();

    writeEvent(res, 'snapshot', await snapshot(pool, batch));

    // Test seam: end right after the snapshot so supertest can assert the body.
    if (req.headers['x-sse-test-oneshot'] === '1') return res.end();

    const bus = getProgressBus(clientFactory);
    const onEvent = async (evt: NotifyPayload) => {
      let card: any = undefined;
      if (evt.event === 'card_identified' && evt.card_id) card = await cardRow(pool, evt.card_id);
      writeEvent(res, 'progress', {
        event: evt.event, card_id: evt.card_id, photo_id: evt.photo_id, card,
      });
      if (evt.event === 'batch_complete') { cleanup(); res.end(); }
    };
    const unsub = bus.subscribe(batch.id, (e) => { void onEvent(e); });

    const hb = setInterval(() => res.write(`: hb\n\n`), 25_000);
    const cleanup = () => { clearInterval(hb); unsub(); };
    req.on('close', cleanup);
  });

  return r;
}
