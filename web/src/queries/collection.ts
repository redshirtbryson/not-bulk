import type { Pool } from "pg";

export interface CollectionRow {
  card_id: string;
  card_ref_id: string | null;
  crop_storage_key: string | null;
  name: string | null;
  set_name: string | null;
  number: string | null;
  finish: string | null;
  quantity: number;
  confidence: number;
  status: string;
  price_cents: number | null;
  price_source: string | null;
  price_fetched_at: string | null;
  batch_id: string;
  has_price_row: boolean;
}

export interface CollectionStats {
  total_cards: number;
  total_value_cents: number;
  priced_fraction: number;
  oldest_price_at: string | null;
}

export interface CollectionFilters {
  batchId?: string;
  set?: string;
  finish?: string;
  source?: "auto" | "corrected";
}

// ORDER BY whitelist. Keys are the only accepted sort values; the SQL fragment is fixed
// text (never interpolated from user input). Unknown -> value_desc.
const SORTS: Record<string, string> = {
  value_desc: "pr.price_cents * c.quantity DESC NULLS LAST",
  name_asc: "r.name ASC",
  set_asc: "r.set_name, r.number",
};

// Build the shared owner + filter WHERE clause with bound params. `start` is the next
// positional index ($1 is always user_id). Returns the clause text and the ordered params.
function whereClause(userId: string, opts: CollectionFilters): { sql: string; params: any[] } {
  const params: any[] = [userId];
  let sql = ` WHERE b.user_id = $1 AND c.status IN ('auto','validated','corrected')`;
  if (opts.batchId) {
    params.push(opts.batchId);
    sql += ` AND b.id = $${params.length}`;
  }
  if (opts.set) {
    params.push(opts.set);
    sql += ` AND r.set_id = $${params.length}`;
  }
  if (opts.finish) {
    params.push(opts.finish);
    sql += ` AND c.finish = $${params.length}`;
  }
  if (opts.source === "auto") {
    sql += ` AND c.status = 'auto'`;
  } else if (opts.source === "corrected") {
    sql += ` AND c.status IN ('validated','corrected')`;
  }
  return { sql, params };
}

export async function getCollection(
  pool: Pool,
  userId: string,
  opts: CollectionFilters & { sort: string; limit: number; offset: number },
): Promise<CollectionRow[]> {
  const { sql: where, params } = whereClause(userId, opts);
  const orderBy = SORTS[opts.sort] ?? SORTS.value_desc;
  params.push(opts.limit);
  const limitIdx = params.length;
  params.push(opts.offset);
  const offsetIdx = params.length;

  const sql =
    `SELECT c.id AS card_id, c.card_ref_id, c.crop_storage_key,
            r.name, r.set_name, r.number,
            c.finish, c.quantity, c.confidence, c.status,
            pr.price_cents, pr.source AS price_source, pr.fetched_at AS price_fetched_at,
            p.batch_id,
            (pr.card_ref_id IS NOT NULL) AS has_price_row
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where +
    ` ORDER BY ${orderBy} LIMIT $${limitIdx} OFFSET $${offsetIdx}`;

  const { rows } = await pool.query(sql, params);
  return rows as CollectionRow[];
}

export async function getCollectionStats(
  pool: Pool,
  userId: string,
  opts: CollectionFilters,
): Promise<CollectionStats> {
  const { sql: where, params } = whereClause(userId, opts);
  const sql =
    `SELECT COALESCE(SUM(c.quantity), 0) AS total_cards,
            COALESCE(SUM(COALESCE(pr.price_cents, 0) * c.quantity), 0) AS total_value_cents,
            CASE WHEN COUNT(*) = 0 THEN 0
                 ELSE COUNT(pr.price_cents)::float / COUNT(*) END AS priced_fraction,
            MIN(pr.fetched_at) AS oldest_price_at
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where;

  const { rows } = await pool.query(sql, params);
  const row = rows[0] ?? {};
  return {
    total_cards: Number(row.total_cards ?? 0),
    total_value_cents: Number(row.total_value_cents ?? 0),
    priced_fraction: Number(row.priced_fraction ?? 0),
    oldest_price_at: row.oldest_price_at ?? null,
  };
}

// Full unpaginated collection for CSV export: same owner + status filter as getCollection,
// deterministic ORDER BY, NO LIMIT/OFFSET (the export is the entire collection).
export async function getCollectionForExport(
  pool: Pool,
  userId: string,
  opts: CollectionFilters,
): Promise<CollectionRow[]> {
  const { sql: where, params } = whereClause(userId, opts);
  const sql =
    `SELECT c.id AS card_id, c.card_ref_id, c.crop_storage_key,
            r.name, r.set_name, r.number,
            c.finish, c.quantity, c.confidence, c.status,
            pr.price_cents, pr.source AS price_source, pr.fetched_at AS price_fetched_at,
            p.batch_id,
            (pr.card_ref_id IS NOT NULL) AS has_price_row
       FROM cards c
       JOIN photos p ON c.photo_id = p.id
       JOIN batches b ON p.batch_id = b.id
       JOIN card_refs r ON c.card_ref_id = r.id
       LEFT JOIN prices pr ON pr.card_ref_id = c.card_ref_id AND pr.finish = c.finish` +
    where +
    ` ORDER BY r.set_name, r.number`;
  const { rows } = await pool.query(sql, params);
  return rows as CollectionRow[];
}
