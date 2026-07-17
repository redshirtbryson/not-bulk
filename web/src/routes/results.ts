import { Router } from 'express';
import type { Pool } from 'pg';
import type { AuthedRequest } from '../middleware/session.js';
import { requireUser } from '../middleware/session.js';
import { getOwnedBatch } from '../queries/batches.js';

export function resultsRouter(pool: Pool): Router {
  const r = Router();
  r.get('/batches/:id/results', requireUser(), async (req: AuthedRequest, res) => {
    const batch = await getOwnedBatch(pool, req.user!.id, req.params.id as string);
    if (!batch) return res.sendStatus(404);

    const cards = (await pool.query(
      `SELECT c.id, r.name, r.set_name, r.number, c.finish, c.confidence, c.quantity, c.status
         FROM cards c JOIN photos p ON p.id=c.photo_id
         LEFT JOIN card_refs r ON r.id=c.card_ref_id
         WHERE p.batch_id=$1 AND c.status IN ('auto','validated','corrected')
         ORDER BY c.confidence DESC, r.name ASC`, [batch.id])).rows;

    const remaining = Number((await pool.query(
      `SELECT count(*)::int AS n FROM cards c JOIN photos p ON p.id=c.photo_id
         WHERE p.batch_id=$1 AND c.status='validation'`, [batch.id])).rows[0]?.n ?? 0);

    const total = cards.reduce((s, c) => s + Number(c.quantity), 0);
    res.render('batch-results.njk', { batch, cards, remaining, total });
  });
  return r;
}
