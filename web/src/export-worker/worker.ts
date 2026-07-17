import type { Pool } from "pg";
import { Client } from "pg";
import type { Config } from "../config.js";
import { Storage } from "../services/storage.js";
import { getCollectionForExport as realGetCollectionForExport } from "../queries/collection.js";
import { formatCents } from "../lib/money.js";
import { renderCollectionPdf as realRenderCollectionPdf, type PdfCard, type PdfStats } from "../lib/pdf.js";
import { claimExportRow, markExportReady, markExportFailed } from "../queries/exports.js";
import { claimExportJob, completeJob, failJob } from "./jobqueue.js";
import { randomUUID } from "node:crypto";

// Injectable seams so the per-job body is unit-testable without a browser/DB.
export interface ExportWorkerDeps {
  getCollectionForExport: typeof realGetCollectionForExport;
  renderCollectionPdf: typeof realRenderCollectionPdf;
}

const DEFAULT_DEPS: ExportWorkerDeps = {
  getCollectionForExport: realGetCollectionForExport,
  renderCollectionPdf: realRenderCollectionPdf,
};

// Row -> PdfCard priceDisplay: null cents (no price row, or a price row with no cents yet)
// renders as "no price data"; otherwise the formatted price.
function priceDisplay(row: { has_price_row: boolean; price_cents: number | null }): string {
  if (row.price_cents == null) return "no price data";
  return formatCents(row.price_cents);
}

/**
 * Render one export end to end. Marks the export row 'failed' AND re-throws on any error
 * so main() can dead-letter the job. No Discord here (see the Node/Python note in the plan).
 */
export async function processExportJob(
  pool: Pool,
  storage: Pick<Storage, "get" | "put">,
  cfg: Config,
  exportId: string,
  deps: ExportWorkerDeps = DEFAULT_DEPS,
): Promise<void> {
  const row = await claimExportRow(pool, exportId);
  if (!row) {
    // Already claimed/gone — nothing to do (idempotent; not an error).
    console.warn(`export ${exportId}: no claimable row, skipping`);
    return;
  }
  try {
    const all = await deps.getCollectionForExport(pool, row.user_id, {});
    const max = cfg.export.max_cards;
    const rows = all.length > max ? all.slice(0, max) : all;
    if (all.length > max) {
      console.warn(`export ${exportId}: collection of ${all.length} truncated to max_cards=${max}`);
    }

    const cards: PdfCard[] = [];
    for (const c of rows) {
      let cropDataUri: string | null = null;
      if (c.crop_storage_key) {
        try {
          const buf = await storage.get(c.crop_storage_key);
          cropDataUri = `data:image/webp;base64,${buf.toString("base64")}`;
        } catch (err) {
          // A missing crop object is non-fatal: render a placeholder rather than fail the whole PDF.
          console.warn(`export ${exportId}: crop ${c.crop_storage_key} unreadable, using placeholder`);
          cropDataUri = null;
        }
      }
      cards.push({
        cropDataUri,
        name: c.name ?? "Unknown",
        set: c.set_name ?? "",
        number: c.number ?? "",
        finish: c.finish ?? "",
        priceDisplay: priceDisplay(c),
        quantity: c.quantity,
      });
    }

    const totalCards = rows.reduce((n, c) => n + c.quantity, 0);
    const totalValueCents = rows.reduce(
      (n, c) => n + (c.price_cents ?? 0) * c.quantity, 0,
    );
    const stats: PdfStats = {
      totalCards,
      totalValueDisplay: formatCents(totalValueCents),
      generatedAt: new Date().toISOString(),
    };

    const buf = await deps.renderCollectionPdf(cards, stats, cfg);
    const storageKey = `${cfg.export.storage_prefix}/${row.user_id}/${exportId}.pdf`;
    await storage.put(storageKey, buf, "application/pdf");

    const expiresAt = new Date(Date.now() + cfg.export.retention_hours * 3600 * 1000);
    await markExportReady(pool, exportId, storageKey, buf.byteLength, cards.length, expiresAt);
  } catch (err) {
    const cls = (err as Error).name || "Error";
    await markExportFailed(pool, exportId, cls);
    console.error(`export ${exportId} failed:`, cls, (err as Error).message);
    throw err;
  }
}

async function runOnce(pool: Pool, storage: Storage, cfg: Config, workerId: string): Promise<boolean> {
  const job = await claimExportJob(pool, workerId);
  if (!job) return false;
  try {
    await processExportJob(pool, storage, cfg, job.payload.export_id);
    await completeJob(pool, job.id);
  } catch (err) {
    // The export row is already marked 'failed' by processExportJob; dead-letter the job row.
    await failJob(pool, job.id, (err as Error).name || "Error", true);
  }
  return true;
}

export async function main(): Promise<void> {
  const { loadConfig } = await import("../config.js");
  const { getPool } = await import("../db.js");
  const cfg = loadConfig();
  const pool = getPool();
  const storage = new Storage(cfg);
  const workerId = `export-${randomUUID()}`;

  // Dedicated LISTEN client (held open for the process lifetime — not from the pool).
  const listen = new Client({ connectionString: process.env.DATABASE_URL });
  await listen.connect();
  await listen.query("LISTEN jobs_wake");

  let running = true;
  let wake: (() => void) | null = null;
  listen.on("notification", () => { if (wake) wake(); });

  const shutdown = async () => {
    running = false;
    if (wake) wake();
    try { await listen.end(); } catch { /* ignore */ }
    try { await pool.end(); } catch { /* ignore */ }
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);

  console.log(`notbulk-export-worker ${workerId} up (LISTEN jobs_wake + 5s poll)`);
  while (running) {
    // Drain all queued export jobs, then sleep until a wake or the 5s fallback.
    while (running && (await runOnce(pool, storage, cfg, workerId))) { /* drain */ }
    if (!running) break;
    await new Promise<void>((resolve) => {
      const t = setTimeout(resolve, 5000);
      wake = () => { clearTimeout(t); resolve(); };
    });
  }
}

// Entry point: only run the loop when executed directly (not when imported by tests).
const isMain = (process.argv[1] && process.argv[1].endsWith("worker.ts")) ||
  (process.argv[1] && process.argv[1].endsWith("worker.js"));
if (isMain) {
  main().catch((err) => {
    console.error("export-worker fatal:", err);
    process.exit(1);
  });
}
