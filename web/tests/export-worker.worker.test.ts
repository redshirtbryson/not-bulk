import { describe, it, expect, vi } from "vitest";
import { FakePool, FakeStorage, testCfg } from "./helpers.js";
import { processExportJob, type ExportWorkerDeps } from "../src/export-worker/worker.js";

// A 1x1 webp-ish byte blob standing in for a crop.
const CROP = Buffer.from("webp-crop-bytes");

function makeCfg(overrides: any = {}) {
  return { ...(testCfg as any), storage: { ...(testCfg as any).storage, bucket: "notbulk" },
    export: { retention_hours: 48, render_timeout_ms: 30000, page_size: "Letter",
              storage_prefix: "exports", max_cards: 5000, ...overrides } };
}

function baseDeps(overrides: Partial<ExportWorkerDeps> = {}): ExportWorkerDeps {
  return {
    getCollectionForExport: vi.fn(async () => [
      { card_id: "c1", crop_storage_key: "user-1/b/crops/c1.webp", name: "Pikachu",
        set_name: "Base", number: "58", finish: "holofoil", quantity: 2, price_cents: 1234,
        has_price_row: true },
    ] as any),
    renderCollectionPdf: vi.fn(async () => Buffer.from("%PDF-1.4 canned")),
    ...overrides,
  };
}

describe("processExportJob", () => {
  it("happy path: claim row -> load collection -> render -> put -> markReady", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    storage.seed("user-1/b/crops/c1.webp", CROP);
    // claimExportRow returns the export row.
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    // markExportReady UPDATE (no rows needed).
    const deps = baseDeps();

    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);

    // renderCollectionPdf received a PdfCard with a data-URI crop + a formatted price.
    const [cards, stats] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards[0].cropDataUri).toBe(`data:image/webp;base64,${CROP.toString("base64")}`);
    expect(cards[0].priceDisplay).toBe("$12.34");
    expect(cards[0].quantity).toBe(2);
    expect(stats.totalCards).toBe(2); // quantity-weighted
    expect(stats.totalValueDisplay).toBe("$24.68");
    // Uploaded to exports/{user}/{export}.pdf as application/pdf.
    expect(storage.puts.at(-1)).toMatchObject({
      key: "exports/user-1/exp-1.pdf", contentType: "application/pdf",
    });
    // markExportReady ran (the last query is the UPDATE ... status='ready').
    const ready = pool.calls.find((c) => /status='ready'/.test(c.sql));
    expect(ready).toBeDefined();
    expect(ready!.params).toContain("exports/user-1/exp-1.pdf");
  });

  it("null crop_storage_key -> cropDataUri null (template renders a placeholder)", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const deps = baseDeps({
      getCollectionForExport: vi.fn(async () => [
        { card_id: "c1", crop_storage_key: null, name: "Missing", set_name: "Base",
          number: "1", finish: "normal", quantity: 1, price_cents: null, has_price_row: false } as any,
      ]),
    });
    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);
    const [cards] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards[0].cropDataUri).toBeNull();
    expect(cards[0].priceDisplay).toBe("no price data");
  });

  it("truncates to cfg.export.max_cards and logs", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const many = Array.from({ length: 3 }, (_, i) => ({
      card_id: `c${i}`, crop_storage_key: null, name: `n${i}`, set_name: "S", number: `${i}`,
      finish: "normal", quantity: 1, price_cents: null, has_price_row: false,
    }));
    const deps = baseDeps({ getCollectionForExport: vi.fn(async () => many as any) });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await processExportJob(pool as any, storage as any, makeCfg({ max_cards: 2 }), "exp-1", deps);
    const [cards] = (deps.renderCollectionPdf as any).mock.calls[0];
    expect(cards.length).toBe(2);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("truncated"));
    warn.mockRestore();
  });

  it("render throws -> markExportFailed with the error class, and re-throws", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [{ id: "exp-1", user_id: "user-1", status: "rendering" }] });
    const boom = new Error("chromium exploded");
    boom.name = "RenderError";
    const deps = baseDeps({ renderCollectionPdf: vi.fn(async () => { throw boom; }) });
    await expect(
      processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps),
    ).rejects.toThrow("chromium exploded");
    const failed = pool.calls.find((c) => /status='failed'/.test(c.sql));
    expect(failed).toBeDefined();
    expect(failed!.params[0]).toBe("RenderError"); // sanitized: error CLASS, not raw message
  });

  it("claimExportRow returns null (already claimed) -> no render, no throw", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    pool.enqueue({ rows: [] }); // claimExportRow -> null
    const deps = baseDeps();
    await processExportJob(pool as any, storage as any, makeCfg(), "exp-1", deps);
    expect((deps.renderCollectionPdf as any).mock.calls.length).toBe(0);
  });
});
