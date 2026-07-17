import { Router } from 'express';
import type { Pool } from 'pg';
import { z } from 'zod';
import type { Config } from '../config.js';
import type { AuthedRequest } from '../middleware/session.js';
import { requireUser } from '../middleware/session.js';
import { getOwnedBatch } from '../queries/batches.js';
import { getOwnedCard } from '../queries/cards.js';
import { enqueue } from '../services/jobs.js';

const FINISHES = ['non-holo', 'reverse', 'holo'] as const;
const validateBody = z.object({
  card_ref_id: z.string().min(1),
  finish: z.enum(FINISHES).optional(),
});

export function validateRouter(pool: Pool, _cfg: Config): Router {
  const r = Router();

  r.get('/batches/:id/validate', requireUser(), async (req: AuthedRequest, res) => {
    const batch = await getOwnedBatch(pool, req.user!.id, req.params.id as string);
    if (!batch) return res.sendStatus(404);

    // Earliest card still needing a human: status='validation'
    // (this covers finish_needs_confirmation cards, which identify.py forces to 'validation').
    const next = await pool.query(
      `SELECT c.id, c.card_ref_id, c.finish, c.finish_needs_confirmation, c.confidence,
              c.status, c.crop_index, c.candidates
         FROM cards c JOIN photos p ON p.id=c.photo_id
         WHERE p.batch_id=$1 AND c.status='validation'
         ORDER BY c.created_at ASC LIMIT 1`,
      [batch.id],
    );
    const card = next.rows[0];
    if (!card) return res.render('validate.njk', { batch, card: null });

    // Top candidate + up to 2 alternates; join card_refs for names (text only — Assembly Resolution 9).
    const ids = [card.card_ref_id, ...(card.candidates ?? []).map((c: any) => c.card_ref_id)].filter(Boolean);
    const uniq = Array.from(new Set(ids)).slice(0, 3);
    const refs = uniq.length
      ? (await pool.query(`SELECT id, name, set_name, number, finishes FROM card_refs WHERE id = ANY($1)`, [uniq]))
          .rows
      : [];
    const byId: Record<string, any> = {};
    for (const ref of refs) byId[ref.id] = ref;
    const options = uniq.map((id) => byId[id]).filter(Boolean);

    res.render('validate.njk', { batch, card, options });
  });

  r.post('/cards/:id/validate', requireUser(), async (req: AuthedRequest, res) => {
    const parsed = validateBody.safeParse(req.body);
    if (!parsed.success) return res.status(400).json({ error: 'invalid body' });
    const { card_ref_id, finish } = parsed.data;

    const card = await getOwnedCard(pool, req.user!.id, req.params.id as string);
    if (!card) return res.sendStatus(404);

    const predicted = card.card_ref_id;
    const newStatus = card_ref_id === predicted ? 'validated' : 'corrected';

    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // Update the card. finish is set only when provided; clearing the flag then too.
      if (finish) {
        await client.query(
          `UPDATE cards SET card_ref_id=$1, status=$2, finish=$3,
                            finish_needs_confirmation=false, updated_at=now()
             WHERE id=$4 RETURNING id`,
          [card_ref_id, newStatus, finish, card.id],
        );
      } else {
        await client.query(
          `UPDATE cards SET card_ref_id=$1, status=$2, updated_at=now()
             WHERE id=$3 RETURNING id`,
          [card_ref_id, newStatus, card.id],
        );
      }

      // Duplicate merge: earliest OTHER card in the same batch with same (ref, finish)
      // already resolved → increment its quantity, mark this card 'merged'.
      const finishForMerge = finish ?? card.finish;
      const dup = await client.query(
        `SELECT c.id FROM cards c JOIN photos p ON p.id=c.photo_id
           WHERE p.batch_id=$1 AND c.id<>$2 AND c.card_ref_id=$3
             AND c.finish IS NOT DISTINCT FROM $4
             AND c.status IN ('validated','corrected','auto')
           ORDER BY c.created_at ASC LIMIT 1`,
        [card.batch_id, card.id, card_ref_id, finishForMerge],
      );
      if (dup.rows[0]) {
        await client.query(`UPDATE cards SET quantity=quantity+1, updated_at=now() WHERE id=$1`, [dup.rows[0].id]);
        await client.query(`UPDATE cards SET status='merged', quantity=0, updated_at=now() WHERE id=$1`, [card.id]);
      }

      // Corrections flywheel: Node does NOT write the corrections row (Assembly Resolution 5).
      // It enqueues ingest_correction with the extended payload; the Python handler
      // computes crop_hash and inserts the corrections row.
      await enqueue(client, {
        type: 'ingest_correction',
        payload: { card_id: card.id, actual_ref_id: card_ref_id, predicted_ref_id: predicted },
        batchId: card.batch_id,
        userId: req.user!.id,
      });

      await client.query('COMMIT');
    } catch (e) {
      await client.query('ROLLBACK');
      throw e;
    } finally {
      client.release();
    }
    await pool.query(`NOTIFY jobs_wake`);

    return res.redirect(302, `/batches/${card.batch_id}/validate`);
  });

  const simpleStatus = (status: 'skipped' | 'not_card') => async (req: AuthedRequest, res: any) => {
    const card = await getOwnedCard(pool, req.user!.id, req.params.id as string);
    if (!card) return res.sendStatus(404);
    await pool.query(`UPDATE cards SET status=$1, updated_at=now() WHERE id=$2`, [status, card.id]);
    return res.redirect(302, `/batches/${card.batch_id}/validate`);
  };

  r.post('/cards/:id/skip', requireUser(), simpleStatus('skipped'));
  r.post('/cards/:id/not-card', requireUser(), simpleStatus('not_card'));

  return r;
}
