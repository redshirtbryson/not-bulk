import { describe, it, expect } from "vitest";
import request from "supertest";
import type { Pool } from "pg";
import { createApp } from "../src/app.js";
import type { Config } from "../src/config.js";

const cfg = {
  web: { port: 3000, base_url: "http://127.0.0.1:3000", secure_cookies: false },
  storage: {
    endpoint: "http://127.0.0.1:9000",
    bucket: "notbulk",
    access_key: "minioadmin",
    secret_key: "minioadmin",
    signed_url_ttl_seconds: 900,
  },
} as unknown as Config;

function fakePool(queryImpl: (sql: string) => Promise<unknown>): Pool {
  return { query: (sql: string) => queryImpl(sql) } as unknown as Pool;
}

describe("createApp", () => {
  it("GET /healthz returns 200 {ok:true} after SELECT 1", async () => {
    let seen = "";
    const app = createApp({ cfg, pool: fakePool(async (sql) => { seen = sql; return { rows: [{ "?column?": 1 }] }; }) });
    const res = await request(app).get("/healthz");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ok: true });
    expect(seen).toBe("SELECT 1");
  });

  it("sets the exact CSP header and nosniff", async () => {
    const app = createApp({ cfg, pool: fakePool(async () => ({ rows: [] })) });
    const res = await request(app).get("/healthz");
    expect(res.headers["content-security-policy"]).toBe(
      "default-src 'self'; img-src 'self' http://127.0.0.1:9000; style-src 'self'; script-src 'self' https://challenges.cloudflare.com; frame-ancestors 'none'",
    );
    expect(res.headers["x-content-type-options"]).toBe("nosniff");
  });

  it("unknown route returns 404 Not Found (no stack)", async () => {
    const app = createApp({ cfg, pool: fakePool(async () => ({ rows: [] })) });
    const res = await request(app).get("/does-not-exist");
    expect(res.status).toBe(404);
    expect(res.text).toBe("Not Found");
  });

  it("serves the vendored htmx script from /vendor/", async () => {
    const app = createApp({ cfg, pool: fakePool(async () => ({ rows: [] })) });
    const res = await request(app).get("/vendor/htmx.min.js");
    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/javascript/);
    expect(res.text).toContain("htmx.org 2.x");
  });
});
