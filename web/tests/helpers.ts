import request from "supertest";
import { createHash, randomBytes } from "node:crypto";
import type { Express } from "express";
import type { RequestHandler } from "express";
import type { Mailer } from "../src/services/mailer.js";
import type { Config } from "../src/config.js";
import type { AppDeps } from "../src/app.js";

export interface QueryResult<R = any> { rows: R[] }

/** A single fake PoolClient: shifts queued results, records every call, no-op release. */
export class FakeClient {
  public calls: Array<{ sql: string; params: any[] }> = [];
  private queue: Array<{ rows: any[] }> = [];
  enqueue(result: { rows: any[] }): void { this.queue.push(result); }
  async query(sql: string, params: any[] = []): Promise<QueryResult> {
    this.calls.push({ sql, params });
    return this.queue.shift() ?? { rows: [] };
  }
  release(): void {}
}

/**
 * FakePool: queue-based pg.Pool fake. `enqueue({rows})` in the order the code issues
 * queries; `.calls` records `{sql, params}`. A single shared FakeClient backs both
 * `pool.query(...)` and `pool.connect()` so transaction + pool-level statements land
 * in one ordered `.calls` log. Unqueued queries default to `{rows: []}`.
 */
export class FakePool {
  public client = new FakeClient();
  get calls() { return this.client.calls; }
  enqueue(result: { rows: any[] }): void { this.client.enqueue(result); }
  async query(sql: string, params: any[] = []): Promise<QueryResult> {
    return this.client.query(sql, params);
  }
  async connect(): Promise<FakeClient> { return this.client; }
}

export class FakeMailer implements Mailer {
  public sent: Array<{ email: string; url: string }> = [];
  async sendMagicLink(email: string, url: string): Promise<void> {
    this.sent.push({ email, url });
  }
}

export class FakeStorage {
  puts: Array<{ key: string; body: Buffer; contentType: string }> = [];
  photoKey(u: string, b: string, p: string) { return `${u}/${b}/${p}.webp`; }
  cropKey(u: string, b: string, c: string) { return `${u}/${b}/crops/${c}.webp`; }
  async put(key: string, body: Buffer, contentType: string) { this.puts.push({ key, body, contentType }); }
  async signedGetUrl(key: string) { return `http://127.0.0.1:9000/notbulk/${key}?sig=canned`; }
  async delete() {}
}

/** Minimal typed Config stub for tests; override fields per test as needed. */
export const testCfg = {
  web: { port: 3000, base_url: "http://127.0.0.1:3000", secure_cookies: false },
  auth: {
    session_absolute_days: 30, session_idle_days: 7, magic_link_expiry_minutes: 15,
    magic_links_per_email_hour: 3, magic_links_per_email_day: 10,
  },
} as unknown as Config;

/** The test cookie name; the default test sessionMiddleware decodes `req.user` from it. */
export const TEST_COOKIE = "nb_session";

/**
 * makeDeps: builds an AppDeps for createApp with a FakePool and sensible fakes.
 * Overrides win. The DEFAULT `sessionMiddleware` is a test seam: it decodes a
 * JSON-encoded user from the `nb_session` cookie into `req.user` WITHOUT touching the
 * pool — so `authedAgent(app, user)` works and the pool's queue stays reserved for the
 * route's own queries. Task 17's session-window tests OVERRIDE `sessionMiddleware` with
 * the real `sessionMiddleware(pool, cfg)` from Task 3 to exercise the DB session lookup.
 */
export function makeDeps(overrides: Partial<AppDeps> = {}): AppDeps {
  const pool = (overrides.pool as any) ?? new FakePool();
  const defaultSession: RequestHandler = (req: any, _res, next) => {
    const raw = req.cookies?.[TEST_COOKIE] ?? cookieFrom(req.headers?.cookie, TEST_COOKIE);
    if (raw) { try { req.user = JSON.parse(Buffer.from(raw, "base64url").toString("utf8")); } catch { /* anon */ } }
    next();
  };
  return {
    cfg: testCfg,
    pool: pool as any,
    storage: (new FakeStorage() as any),
    mailer: new FakeMailer(),
    sessionMiddleware: overrides.sessionMiddleware ?? defaultSession,
    ...overrides,
  };
}

function cookieFrom(header: string | undefined, name: string): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const [k, v] = part.trim().split("=");
    if (k === name) return v;
  }
  return undefined;
}

/** Encode a user object into the base64url test-cookie value the default seam decodes. */
export function encodeUser(user: { id: string; email?: string | null; tier?: string }): string {
  return Buffer.from(JSON.stringify({ email: null, tier: "free", ...user }), "utf8").toString("base64url");
}

/** authedAgent: a supertest agent carrying an nb_session cookie the default seam maps to `user`. */
export function authedAgent(app: Express, user: { id: string; email?: string | null; tier?: string }) {
  const agent = request.agent(app);
  (agent as any).set("Cookie", `${TEST_COOKIE}=${encodeUser(user)}`);
  return agent;
}

/** anonAgent: a supertest agent with no session cookie. */
export function anonAgent(app: Express) { return request.agent(app); }

/**
 * makeSession: enqueue a valid session-lookup row (both windows hold) for `user` so the
 * REAL `sessionMiddleware(pool, cfg)` (Task 17 override) resolves it. Returns `{ token }`
 * — an opaque cookie value tests set explicitly via `.set('Cookie', ...)`.
 */
export function makeSession(pool: FakePool, user: { id: string; email?: string | null; tier?: string }): { token: string } {
  const token = randomBytes(32).toString("hex");
  pool.enqueue({ rows: [{ session_id: "s-" + user.id, user_id: user.id, email: user.email ?? "user@example.com", tier: user.tier ?? "free" }] });
  return { token };
}

/** expireSession: enqueue a 0-row session lookup for `token` (absolute window blown → SQL filters it). */
export function expireSession(pool: FakePool, _token?: string) { pool.enqueue({ rows: [] }); }

/** idleSession: enqueue a 0-row session lookup for `token` (idle window blown → SQL predicate filters it). */
export function idleSession(pool: FakePool, _token?: string) { pool.enqueue({ rows: [] }); }

/** rows() helper: build a QueryResult from row objects. */
export function rows<R>(...r: R[]): QueryResult<R> { return { rows: r }; }

/** sha256 hex helper for session/token-hash assertions. */
export function sha256hex(s: string): string { return createHash("sha256").update(s).digest("hex"); }
