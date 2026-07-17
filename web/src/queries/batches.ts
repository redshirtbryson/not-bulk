import type { Pool } from "pg";

export interface BatchRow {
  id: string;
  user_id: string;
  status: string;
  photo_count: number;
  origin_url: string | null;
  created_at: string;
}

// Contract helper: direct owned-batch lookup (batches.user_id filter).
export async function getOwnedBatch(
  pool: Pool,
  userId: string,
  batchId: string,
): Promise<BatchRow | null> {
  const { rows } = await pool.query(
    `SELECT b.id, b.user_id, b.status, b.photo_count, b.origin_url, b.created_at
       FROM batches b
      WHERE b.id = $1 AND b.user_id = $2`,
    [batchId, userId],
  );
  return rows[0] ?? null;
}

export interface OwnedPhoto {
  id: string;
  storage_key: string | null;
}

export async function getOwnedPhoto(
  pool: Pool,
  userId: string,
  photoId: string,
): Promise<OwnedPhoto | null> {
  const { rows } = await pool.query(
    `SELECT p.id, p.storage_key
       FROM photos p
       JOIN batches b ON b.id = p.batch_id
      WHERE p.id = $1 AND b.user_id = $2`,
    [photoId, userId],
  );
  return rows[0] ?? null;
}
