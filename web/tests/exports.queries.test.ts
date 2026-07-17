import { describe, it, expect } from "vitest";
import { Readable } from "node:stream";
import { Storage } from "../src/services/storage.js";
import { testCfg, FakePool } from "./helpers.js";
import {
  createExport,
  getOwnedExport,
  claimExportRow,
  markExportReady,
  markExportFailed,
} from "../src/queries/exports.js";

describe("Storage.get", () => {
  it("reads an object body into a Buffer", async () => {
    const storage = new Storage(testCfg as any);
    // Inject a fake S3 client: GetObjectCommand -> a body that is an async iterable stream.
    (storage as any).client = {
      send: async () => ({ Body: Readable.from([Buffer.from("hello "), Buffer.from("pdf")]) }),
    };
    const buf = await storage.get("exports/u/e.pdf");
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.toString("utf8")).toBe("hello pdf");
  });
});

describe("queries/exports", () => {
  it("createExport inserts a queued row and returns its id", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1" }] });
    const id = await createExport(pool as any, "user-1", "pdf");
    expect(id).toBe("exp-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/INSERT INTO exports/i);
    expect(sql).toMatch(/'queued'/);
    expect(params).toContain("user-1");
    expect(params).toContain("pdf");
  });

  it("getOwnedExport filters by user_id and returns the row", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "ready" }] });
    const row = await getOwnedExport(pool as any, "user-1", "exp-1");
    expect(row?.id).toBe("exp-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/WHERE id=\$1 AND user_id=\$2/i);
    expect(params).toEqual(["exp-1", "user-1"]);
  });

  it("getOwnedExport returns null when not owned (0 rows)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    expect(await getOwnedExport(pool as any, "user-2", "exp-1")).toBeNull();
  });

  it("claimExportRow sets status='rendering' and returns the row", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const row = await claimExportRow(pool as any, "exp-1");
    expect(row?.status).toBe("rendering");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE exports SET status='rendering'/i);
    expect(params).toEqual(["exp-1"]);
  });

  it("markExportReady writes storage_key, bytes, card_count, status='ready', expires_at", async () => {
    const pool = new FakePool();
    const expires = new Date("2026-07-19T00:00:00Z");
    await markExportReady(pool as any, "exp-1", "exports/user-1/exp-1.pdf", 4096, 12, expires);
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE exports SET/i);
    expect(sql).toMatch(/status='ready'/);
    expect(params).toEqual(["exports/user-1/exp-1.pdf", 4096, 12, expires, "exp-1"]);
  });

  it("markExportFailed writes status='failed' + last_error", async () => {
    const pool = new FakePool();
    await markExportFailed(pool as any, "exp-1", "RenderTimeout");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/status='failed'/);
    expect(params).toEqual(["RenderTimeout", "exp-1"]);
  });
});
