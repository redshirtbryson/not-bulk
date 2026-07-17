import type { Pool } from "pg";
import { uuidv7 } from "uuidv7";

export interface ExportRow {
  id: string;
  user_id: string;
  kind: string;
  status: string;
  storage_key: string | null;
  card_count: number;
  bytes: number;
  last_error: string | null;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export async function createExport(pool: Pool, userId: string, kind: string): Promise<string> {
  const id = uuidv7();
  const { rows } = await pool.query(
    `INSERT INTO exports (id, user_id, kind, status)
     VALUES ($1, $2, $3, 'queued') RETURNING id`,
    [id, userId, kind],
  );
  return rows[0].id as string;
}

export async function getOwnedExport(
  pool: Pool,
  userId: string,
  exportId: string,
): Promise<ExportRow | null> {
  const { rows } = await pool.query(
    `SELECT id, user_id, kind, status, storage_key, card_count, bytes,
            last_error, expires_at, created_at, updated_at
       FROM exports WHERE id=$1 AND user_id=$2`,
    [exportId, userId],
  );
  return (rows[0] as ExportRow) ?? null;
}

export async function claimExportRow(pool: Pool, exportId: string): Promise<ExportRow | null> {
  const { rows } = await pool.query(
    `UPDATE exports SET status='rendering', updated_at=now()
       WHERE id=$1
     RETURNING id, user_id, kind, status, storage_key, card_count, bytes,
               last_error, expires_at, created_at, updated_at`,
    [exportId],
  );
  return (rows[0] as ExportRow) ?? null;
}

export async function markExportReady(
  pool: Pool,
  exportId: string,
  storageKey: string,
  bytes: number,
  cardCount: number,
  expiresAt: Date,
): Promise<void> {
  await pool.query(
    `UPDATE exports SET status='ready', storage_key=$1, bytes=$2, card_count=$3,
            expires_at=$4, updated_at=now()
       WHERE id=$5`,
    [storageKey, bytes, cardCount, expiresAt, exportId],
  );
}

export async function markExportFailed(pool: Pool, exportId: string, error: string): Promise<void> {
  await pool.query(
    `UPDATE exports SET status='failed', last_error=$1, updated_at=now() WHERE id=$2`,
    [error, exportId],
  );
}
