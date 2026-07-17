import { Router } from 'express';
import type { Pool } from 'pg';
import type { AuthedRequest } from '../middleware/session.js';
import { requireUser } from '../middleware/session.js';

export function searchRouter(pool: Pool): Router {
  const r = Router();

  r.get('/api/search-refs', requireUser(), async (req: AuthedRequest, res) => {
    const q = String(req.query.q ?? '').trim();
    if (!q) return res.json([]);
    // Prefix match on lower(name) OR exact number; all input bound as params.
    const rows = (
      await pool.query(
        `SELECT id, name, set_name, number FROM card_refs
         WHERE lower(name) LIKE $1 OR number = $2
         ORDER BY name ASC LIMIT 10`,
        [q.toLowerCase() + '%', q],
      )
    ).rows;

    // htmx requests get the partial; plain requests get JSON.
    if (req.headers['hx-request'] === 'true') {
      return res.render('partials/search-results.njk', { results: rows });
    }
    return res.json(rows);
  });

  return r;
}
