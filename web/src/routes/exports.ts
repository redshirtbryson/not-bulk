import { Router } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { AuthedRequest } from "../middleware/session.js";
import { requireUser } from "../middleware/session.js";
import { Storage } from "../services/storage.js";
import { createExport, getOwnedExport } from "../queries/exports.js";
import { enqueue } from "../services/jobs.js";

export function exportsRouter(pool: Pool, cfg: Config, storageArg?: Storage): Router {
  const r = Router();
  const storage = storageArg ?? new Storage(cfg);

  // Kick off an async PDF export: create the row + enqueue the job in one txn, then wake a worker.
  r.post("/collection/export.pdf", requireUser(), async (req: AuthedRequest, res, next) => {
    const userId = req.user!.id;
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      const exportId = await createExport(client as unknown as Pool, userId, "pdf");
      await enqueue(client, {
        type: "export",
        payload: { export_id: exportId },
        userId,
      });
      await client.query("COMMIT");
      await pool.query("NOTIFY jobs_wake");
      return res.redirect(302, `/collection/exports/${exportId}`);
    } catch (err) {
      await client.query("ROLLBACK");
      return next(err);
    } finally {
      client.release();
    }
  });

  // Status page: queued/rendering (self-refreshing) -> ready (download) / failed (error).
  r.get("/collection/exports/:id", requireUser(), async (req: AuthedRequest, res) => {
    const row = await getOwnedExport(pool, req.user!.id, req.params.id as string);
    if (!row) return res.status(404).send("export not found");
    const terminal = row.status === "ready" || row.status === "failed";
    return res.render("export-status.njk", { export: row, terminal });
  });

  // Owner-checked, freshness-checked redirect to a short-lived signed MinIO URL.
  r.get("/collection/exports/:id/download", requireUser(), async (req: AuthedRequest, res) => {
    const row = await getOwnedExport(pool, req.user!.id, req.params.id as string);
    if (!row) return res.status(404).send("export not found");
    if (row.status !== "ready" || !row.storage_key) return res.status(409).send("export not ready");
    if (row.expires_at && new Date(row.expires_at).getTime() <= Date.now()) {
      return res.status(410).send("export expired");
    }
    const url = await storage.signedGetUrl(row.storage_key);
    return res.redirect(302, url);
  });

  return r;
}
