import type { Pool, PoolClient } from 'pg';
import type { Config } from '../config.js';

type Db = Pool | PoolClient;
type Want = { batches?: number; photos?: number; fetches?: number };

export async function checkAndReserve(
  pool: Db,
  cfg: Config,
  userId: string,
  want: Want,
): Promise<{ ok: boolean; reason?: string }> {
  const b = want.batches ?? 0;
  const p = want.photos ?? 0;
  const f = want.fetches ?? 0;
  const capB = cfg.quotas.batches_per_day;
  const capP = cfg.quotas.photos_per_day;
  const capF = cfg.quotas.fetches_per_day;

  // Atomic conditional upsert. Insert-with-CURRENT_DATE creates today's row on
  // first use; on conflict we add the requested amounts, but the WHERE guard
  // only lets the UPDATE proceed (and RETURN) when all post-add totals are
  // within caps. Params: [userId, b, p, f, capB, capP, capF].
  const upsert = `
    INSERT INTO usage (user_id, day, batches, photos, fetches)
    VALUES ($1, CURRENT_DATE, $2, $3, $4)
    ON CONFLICT (user_id, day) DO UPDATE SET
      batches = usage.batches + EXCLUDED.batches,
      photos  = usage.photos  + EXCLUDED.photos,
      fetches = usage.fetches + EXCLUDED.fetches,
      day = CURRENT_DATE
    WHERE usage.batches + EXCLUDED.batches <= $5
      AND usage.photos  + EXCLUDED.photos  <= $6
      AND usage.fetches + EXCLUDED.fetches <= $7
    RETURNING user_id`;

  const { rows } = await pool.query(upsert, [userId, b, p, f, capB, capP, capF]);
  if (rows.length > 0) return { ok: true };

  // Blocked: re-read current counts to name the exceeded dimension.
  const { rows: cur } = await pool.query(
    `SELECT COALESCE(batches,0) AS batches,
            COALESCE(photos,0)  AS photos,
            COALESCE(fetches,0) AS fetches
       FROM usage WHERE user_id = $1 AND day = CURRENT_DATE`,
    [userId],
  );
  const now = cur[0] ?? { batches: 0, photos: 0, fetches: 0 };
  let reason = 'quota';
  if (now.batches + b > capB) reason = 'batches';
  else if (now.photos + p > capP) reason = 'photos';
  else if (now.fetches + f > capF) reason = 'fetches';
  return { ok: false, reason };
}
