import { randomBytes } from "node:crypto";
import { uuidv7 } from "uuidv7";
import { z } from "zod";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { Mailer } from "../services/mailer.js";
import { isDisposable } from "../services/blocklist.js";
import { upsertUserByEmail } from "../queries/users.js";
import { createSession, sha256hex } from "./sessions.js";

const emailSchema = z.string().email().max(254);

/**
 * Always resolves void (constant-time-ish: never reveals whether the email was
 * accepted, blocked, or rate-limited). Enforces blocklist + rate limits internally.
 */
export async function requestMagicLink(
  pool: Pool,
  cfg: Config,
  mailer: Mailer,
  rawEmail: string,
): Promise<void> {
  const email = rawEmail.trim().toLowerCase();
  const parsed = emailSchema.safeParse(email);
  if (!parsed.success) return;
  if (isDisposable(email)) return;

  // Single query returns both windows' counts.
  const counts = await pool.query(
    `SELECT
        count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS hour_count,
        count(*) FILTER (WHERE created_at > now() - interval '1 day')  AS day_count
       FROM magic_links WHERE email = $1`,
    [email],
  );
  const hourCount = Number(counts.rows[0].hour_count);
  const dayCount = Number(counts.rows[0].day_count);
  if (hourCount >= cfg.auth.magic_links_per_email_hour) return;
  if (dayCount >= cfg.auth.magic_links_per_email_day) return;

  const token = randomBytes(32).toString("base64url");
  const expiryMs = cfg.auth.magic_link_expiry_minutes * 60_000;
  await pool.query(
    `INSERT INTO magic_links (id, email, token_hash, expires_at)
     VALUES ($1, $2, $3, now() + ($4::bigint * interval '1 millisecond'))`,
    [uuidv7(), email, sha256hex(token), expiryMs],
  );

  const url = `${cfg.web.base_url}/auth/verify?token=${encodeURIComponent(token)}`;
  await mailer.sendMagicLink(email, url);
}

/**
 * Single-use verify. Returns an opaque session cookie token, or null when the
 * link is invalid/expired/already-used. UPDATE ... RETURNING guarantees atomic
 * single-use.
 */
export async function verifyMagicLink(pool: Pool, cfg: Config, token: string): Promise<string | null> {
  const consumed = await pool.query(
    `UPDATE magic_links SET used_at = now()
      WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
      RETURNING email`,
    [sha256hex(token)],
  );
  // Use rows.length rather than rowCount: the canonical FakePool/FakeClient test
  // double only returns { rows }, so rowCount is always undefined against it.
  if (consumed.rows.length === 0) return null;
  const email = consumed.rows[0].email as string;

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const user = await upsertUserByEmail(client, email);
    await client.query("COMMIT");
    return await createSession(pool, cfg, user.id);
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

export { sha256hex };
