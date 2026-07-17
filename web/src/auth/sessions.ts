import { createHash, randomBytes } from "node:crypto";
import { uuidv7 } from "uuidv7";
import type { Pool } from "pg";
import type { Config } from "../config.js";

export function sha256hex(s: string): string {
  return createHash("sha256").update(s).digest("hex");
}

/** Create a session; returns the opaque cookie token (NOT stored — only its hash is). */
export async function createSession(pool: Pool, cfg: Config, userId: string): Promise<string> {
  const token = randomBytes(32).toString("base64url");
  const absoluteMs = cfg.auth.session_absolute_days * 86400_000;
  await pool.query(
    `INSERT INTO sessions (id, user_id, token_hash, expires_at)
     VALUES ($1, $2, $3, now() + ($4::bigint * interval '1 millisecond'))`,
    [uuidv7(), userId, sha256hex(token), absoluteMs],
  );
  return token;
}

export async function destroySession(pool: Pool, token: string): Promise<void> {
  await pool.query(`DELETE FROM sessions WHERE token_hash = $1`, [sha256hex(token)]);
}

export interface SessionLookup { session_id: string; user_id: string; email: string | null; tier: string }

/**
 * Look up a live session by cookie token. Enforces BOTH windows:
 *   - absolute: expires_at > now()
 *   - idle:     last_seen_at > now() - idle_days
 * Returns null when either window is blown.
 */
export async function lookupSession(pool: Pool, cfg: Config, token: string): Promise<SessionLookup | null> {
  const idleMs = cfg.auth.session_idle_days * 86400_000;
  const res = await pool.query(
    `SELECT s.id AS session_id, s.user_id, u.email, u.tier
       FROM sessions s JOIN users u ON u.id = s.user_id
      WHERE s.token_hash = $1
        AND s.expires_at > now()
        AND s.last_seen_at > now() - ($2::bigint * interval '1 millisecond')
        AND u.status = 'active'`,
    [sha256hex(token), idleMs],
  );
  return (res.rows[0] as SessionLookup) ?? null;
}

/** Sliding touch: advance last_seen_at, at most once per hour (idempotent enough). */
export async function touchSession(pool: Pool, sessionId: string): Promise<void> {
  await pool.query(
    `UPDATE sessions SET last_seen_at = now()
      WHERE id = $1 AND last_seen_at < now() - interval '1 hour'`,
    [sessionId],
  );
}
