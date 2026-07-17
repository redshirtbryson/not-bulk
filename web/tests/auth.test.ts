import { describe, it, expect, beforeEach } from "vitest";
import type { Pool } from "pg";
import { FakePool, FakeMailer, rows } from "./helpers.js";
import { requestMagicLink, verifyMagicLink } from "../src/auth/magic.js";
import { lookupSession, createSession } from "../src/auth/sessions.js";
import type { Config } from "../src/config.js";

const cfg = {
  web: { base_url: "http://127.0.0.1:3000", secure_cookies: false },
  auth: {
    session_absolute_days: 30,
    session_idle_days: 7,
    magic_link_expiry_minutes: 15,
    magic_links_per_email_hour: 3,
    magic_links_per_email_day: 10,
  },
} as unknown as Config;

describe("requestMagicLink", () => {
  let mailer: FakeMailer;
  beforeEach(() => { mailer = new FakeMailer(); });

  it("sends when under the limit and lowercases the email", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ hour_count: "0", day_count: "0" }] }); // rate-limit count
    pool.enqueue({ rows: [] });                                    // INSERT INTO magic_links
    await requestMagicLink(pool as unknown as Pool, cfg, mailer, "  USER@Example.COM ");
    expect(mailer.sent).toHaveLength(1);
    expect(mailer.sent[0].email).toBe("user@example.com");
    expect(mailer.sent[0].url).toContain("/auth/verify?token=");
    expect(pool.calls[0].sql).toContain("count(*) FILTER");
    expect(pool.calls[1].sql).toContain("INSERT INTO magic_links");
  });

  it("4th request in the hour sends nothing but still resolves void", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ hour_count: "3", day_count: "3" }] }); // over hourly cap
    const result = await requestMagicLink(pool as unknown as Pool, cfg, mailer, "user@example.com");
    expect(result).toBeUndefined();
    expect(mailer.sent).toHaveLength(0);
    expect(pool.calls).toHaveLength(1);                            // no INSERT issued
  });

  it("over the daily cap sends nothing but still resolves void", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ hour_count: "1", day_count: "10" }] }); // over daily cap
    await requestMagicLink(pool as unknown as Pool, cfg, mailer, "user@example.com");
    expect(mailer.sent).toHaveLength(0);
    expect(pool.calls).toHaveLength(1);
  });

  it("invalid email resolves void, no queries issued", async () => {
    const pool = new FakePool(); // nothing enqueued; no query should be issued
    await expect(requestMagicLink(pool as unknown as Pool, cfg, mailer, "not-an-email")).resolves.toBeUndefined();
    expect(pool.calls).toHaveLength(0);
    expect(mailer.sent).toHaveLength(0);
  });
});

describe("verifyMagicLink single-use", () => {
  it("returns a cookie token when the link is fresh", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ email: "user@example.com" }] });                                   // UPDATE magic_links SET used_at RETURNING email
    pool.enqueue({ rows: [] });                                                                // BEGIN (shared queue: client.query() draws from the same FakeClient)
    pool.enqueue({ rows: [{ id: "u1", email: "user@example.com", tier: "free", status: "active" }] }); // INSERT INTO users (upsert) RETURNING
    pool.enqueue({ rows: [] });                                                                // COMMIT
    pool.enqueue({ rows: [] });                                                                // INSERT INTO sessions
    const token = await verifyMagicLink(pool as unknown as Pool, cfg, "raw-token");
    expect(typeof token).toBe("string");
    expect((token as string).length).toBeGreaterThan(20);
    expect(pool.calls[0].sql).toContain("UPDATE magic_links SET used_at");
  });

  it("returns null when already used or expired (UPDATE matches 0 rows)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // UPDATE magic_links matches 0 rows
    const token = await verifyMagicLink(pool as unknown as Pool, cfg, "stale-token");
    expect(token).toBeNull();
  });
});

describe("session windows", () => {
  it("createSession stores only the hash and returns the raw token", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // INSERT INTO sessions
    const token = await createSession(pool as unknown as Pool, cfg, "u1");
    const storedHash = pool.calls[0].params[2]; // token_hash bind param
    expect(pool.calls[0].sql).toContain("INSERT INTO sessions");
    expect(storedHash).not.toBe(token);       // hash != raw
    expect(storedHash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("lookupSession returns null when idle window is blown (query returns 0 rows)", async () => {
    // The idle predicate lives in SQL; a blown window => 0 rows from the DB.
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const s = await lookupSession(pool as unknown as Pool, cfg, "tok");
    expect(s).toBeNull();
    expect(pool.calls[0].sql).toContain("FROM sessions s JOIN users u");
  });

  it("lookupSession returns the session when both windows hold", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ session_id: "s1", user_id: "u1", email: "user@example.com", tier: "free" }] });
    const s = await lookupSession(pool as unknown as Pool, cfg, "tok");
    expect(s?.user_id).toBe("u1");
  });
});
