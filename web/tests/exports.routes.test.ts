import { describe, it, expect } from "vitest";
import { exportPayload } from "../src/services/jobs.js";

describe("jobs export payload schema", () => {
  it("accepts { export_id } and rejects extras / missing", () => {
    expect(() => exportPayload.parse({ export_id: "exp-1" })).not.toThrow();
    expect(() => exportPayload.parse({})).toThrow();
    expect(() => exportPayload.parse({ export_id: "exp-1", extra: 1 })).toThrow();
  });
});

import { createApp } from "../src/app.js";
import { makeDeps, FakePool, FakeStorage, authedAgent, testCfg } from "./helpers.js";

const USER = { id: "user-1", email: "u@test.local", tier: "free" };
const OTHER = { id: "user-2", email: "o@test.local", tier: "free" };

function future() { return new Date(Date.now() + 3600_000).toISOString(); }
function past() { return new Date(Date.now() - 3600_000).toISOString(); }

describe("POST /collection/export.pdf", () => {
  it("creates an export, enqueues an 'export' job, NOTIFYs, and 302s to the status page", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });                    // BEGIN
    pool.enqueue({ rows: [{ id: "exp-1" }] });      // createExport RETURNING id (inside the txn)
    pool.enqueue({ rows: [{ id: "job-1" }] });      // enqueue() INSERT ... RETURNING id
    pool.enqueue({ rows: [] });                     // COMMIT
    const app = createApp(makeDeps({ pool: pool as any }));
    const res = await authedAgent(app, USER).post("/collection/export.pdf");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/collection/exports/exp-1");
    // Ordering: BEGIN -> INSERT exports -> INSERT jobs (export) -> COMMIT -> NOTIFY.
    const sqls = pool.calls.map((c) => c.sql);
    const iBegin = sqls.findIndex((s) => /BEGIN/i.test(s));
    const iExport = sqls.findIndex((s) => /INSERT INTO exports/i.test(s));
    const iJob = sqls.findIndex((s) => /INSERT INTO jobs/i.test(s));
    const iCommit = sqls.findIndex((s) => /COMMIT/i.test(s));
    const iNotify = sqls.findIndex((s) => /NOTIFY jobs_wake/i.test(s));
    expect(iBegin).toBeGreaterThanOrEqual(0);
    expect(iBegin).toBeLessThan(iExport);
    expect(iExport).toBeLessThan(iJob);
    expect(iJob).toBeLessThan(iCommit);
    expect(iCommit).toBeLessThan(iNotify);
    // The job row carries type='export' and the export_id payload.
    const jobCall = pool.calls.find((c) => /INSERT INTO jobs/i.test(c.sql))!;
    expect(jobCall.params).toContain("export");
    expect(JSON.stringify(jobCall.params)).toContain("exp-1");
  });

  it("requires a user (302 to login when anon)", async () => {
    const app = createApp(makeDeps());
    const res = await (await import("supertest")).default(app).post("/collection/export.pdf");
    expect([302, 401]).toContain(res.status);
  });
});

describe("GET /collection/exports/:id (status page)", () => {
  async function renderStatus(row: any, user = USER) {
    const pool = new FakePool();
    pool.enqueue({ rows: [row] }); // getOwnedExport
    const app = createApp(makeDeps({ pool: pool as any }));
    return authedAgent(app, user).get(`/collection/exports/${row.id}`);
  }

  it("renders 'queued' with a meta-refresh and no download link", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "queued", storage_key: null, expires_at: null, last_error: null });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/http-equiv=["']?refresh/i);
    expect(res.text).not.toMatch(/\/download/);
  });

  it("renders 'rendering' with a meta-refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "rendering", storage_key: null, expires_at: null, last_error: null });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/rendering/i);
    expect(res.text).toMatch(/http-equiv=["']?refresh/i);
  });

  it("renders 'ready' with a download link and NO meta-refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: future(), last_error: null, card_count: 3, bytes: 4096 });
    expect(res.status).toBe(200);
    expect(res.text).toContain("/collection/exports/exp-1/download");
    expect(res.text).not.toMatch(/http-equiv=["']?refresh/i);
  });

  it("renders 'failed' with the last_error and no refresh", async () => {
    const res = await renderStatus({ id: "exp-1", user_id: "user-1", status: "failed", storage_key: null, expires_at: null, last_error: "RenderError" });
    expect(res.status).toBe(200);
    expect(res.text).toMatch(/failed/i);
    expect(res.text).toContain("RenderError");
    expect(res.text).not.toMatch(/http-equiv=["']?refresh/i);
  });

  it("404s when the export is not owned by the caller", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] }); // getOwnedExport -> null (IDOR: user-2 asks for user-1's export)
    const app = createApp(makeDeps({ pool: pool as any }));
    const res = await authedAgent(app, OTHER).get("/collection/exports/exp-1");
    expect(res.status).toBe(404);
  });
});

describe("GET /collection/exports/:id/download", () => {
  function appWith(row: any | null) {
    const pool = new FakePool();
    pool.enqueue({ rows: row ? [row] : [] }); // getOwnedExport
    return createApp(makeDeps({ pool: pool as any, storage: new FakeStorage() as any }));
  }

  it("302s to the signed URL when ready + unexpired", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: future() });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(302);
    expect(res.headers.location).toContain("exports/user-1/exp-1.pdf");
    expect(res.headers.location).toContain("sig=canned");
  });

  it("410s when the export has expired", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "ready", storage_key: "exports/user-1/exp-1.pdf", expires_at: past() });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(410);
  });

  it("409s when not yet ready", async () => {
    const app = appWith({ id: "exp-1", user_id: "user-1", status: "rendering", storage_key: null, expires_at: null });
    const res = await authedAgent(app, USER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(409);
  });

  it("404s when not owned (IDOR on download)", async () => {
    const app = appWith(null);
    const res = await authedAgent(app, OTHER).get("/collection/exports/exp-1/download");
    expect(res.status).toBe(404);
  });
});
