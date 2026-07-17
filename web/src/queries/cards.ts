import type { Pool } from "pg";

export interface CardRow {
  id: string;
  photo_id: string;
  crop_index: number;
  crop_storage_key: string | null;
  card_ref_id: string | null;
  finish: string | null;
  finish_needs_confirmation: boolean;
  quantity: number;
  confidence: number;
  status: string;
  candidates: unknown;
}

// Contract helper: owned-card lookup via card→photo→batch join (batches.user_id filter).
export async function getOwnedCard(
  pool: Pool,
  userId: string,
  cardId: string,
): Promise<CardRow | null> {
  const { rows } = await pool.query(
    `SELECT c.id, c.photo_id, c.crop_index, c.crop_storage_key, c.card_ref_id,
            c.finish, c.finish_needs_confirmation, c.quantity, c.confidence,
            c.status, c.candidates
       FROM cards c
       JOIN photos p ON p.id = c.photo_id
       JOIN batches b ON b.id = p.batch_id
      WHERE c.id = $1 AND b.user_id = $2`,
    [cardId, userId],
  );
  return rows[0] ?? null;
}

export interface OwnedCardCrop {
  id: string;
  crop_storage_key: string | null;
}

export async function getOwnedCardCrop(
  pool: Pool,
  userId: string,
  cardId: string,
): Promise<OwnedCardCrop | null> {
  const { rows } = await pool.query(
    `SELECT c.id, c.crop_storage_key
       FROM cards c
       JOIN photos p ON p.id = c.photo_id
       JOIN batches b ON b.id = p.batch_id
      WHERE c.id = $1 AND b.user_id = $2`,
    [cardId, userId],
  );
  return rows[0] ?? null;
}
