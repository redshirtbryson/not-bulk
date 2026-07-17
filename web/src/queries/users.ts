import type { Pool, PoolClient } from "pg";
import { uuidv7 } from "uuidv7";

export interface UserRow { id: string; email: string | null; tier: string; status: string }

/** Upsert a real (email) user; returns the row. Runs inside the caller's txn. */
export async function upsertUserByEmail(
  client: PoolClient,
  email: string,
): Promise<UserRow> {
  const res = await client.query(
    `INSERT INTO users (id, email, tier, status)
     VALUES ($1, $2, 'free', 'active')
     ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
     RETURNING id, email, tier, status`,
    [uuidv7(), email],
  );
  return res.rows[0] as UserRow;
}

export async function getUserById(pool: Pool, id: string): Promise<UserRow | null> {
  const res = await pool.query(
    `SELECT id, email, tier, status FROM users WHERE id = $1`,
    [id],
  );
  return (res.rows[0] as UserRow) ?? null;
}
