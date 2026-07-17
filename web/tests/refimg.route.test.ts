import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { FakePool, FakeStorage, makeDeps } from "./helpers.js";

// Stub session middleware: force a fixed authed user (mirrors images.test.ts).
const AUTHED_USER = { id: "u1", email: "a@b.com", tier: "free" };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

describe("GET /img/ref/:cardRefId", () => {
  it("302 → signed URL when the ref image is already cached", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: "refs/ref-1.webp", image_url: "https://images.pokemontcg.io/x/1.png" }] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get("/img/ref/ref-1");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("http://127.0.0.1:9000/notbulk/refs/ref-1.webp?sig=canned");
  });

  it("404 when ensureRefCached returns null (card_ref missing)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const storage = new FakeStorage();
    const app = createApp(makeDeps({ pool, storage: storage as any, sessionMiddleware: withUser() }));
    const res = await request(app).get("/img/ref/missing");
    expect(res.status).toBe(404);
  });

  it("302 redirects anonymous users to / (requireUser gate on /img)", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    // Default session seam + no cookie → req.user unset → requireUser 302 → "/".
    const app = createApp(makeDeps({ pool, storage: storage as any }));
    const res = await request(app).get("/img/ref/ref-1");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
