import { Router } from "express";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { AuthedRequest } from "../middleware/session.js";
import { requireUser } from "../middleware/session.js";
import { getCollection, getCollectionStats, type CollectionFilters, type CollectionRow } from "../queries/collection.js";
import { formatCents } from "../lib/money.js";

const SORTS = new Set(["value_desc", "name_asc", "set_asc"]);

// Read one string query param, treating "" as absent.
function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

// Parse the shared filter params (batch/set/finish/source) into a CollectionFilters.
function parseFilters(q: any): CollectionFilters {
  const source = q.source === "auto" || q.source === "corrected" ? q.source : undefined;
  return { batchId: str(q.batch), set: str(q.set), finish: str(q.finish), source };
}

// View-only display string for a row's price: "pending price" (no prices row yet),
// "no price data" (row exists, price_cents NULL), or the formatted dollar amount.
// The ONLY place explorer rows get their money string — never format in the template.
function priceDisplay(row: CollectionRow): string {
  if (!row.has_price_row) return "pending price";
  if (row.price_cents == null) return "no price data";
  return formatCents(row.price_cents);
}

export function collectionRouter(pool: Pool, cfg: Config): Router {
  const r = Router();

  r.get("/collection", requireUser(), async (req: AuthedRequest, res) => {
    const q = req.query as any;
    const filters = parseFilters(q);
    const sort = SORTS.has(q.sort) ? (q.sort as string) : cfg.explorer.default_sort;
    const pageSize = cfg.explorer.page_size;
    const page = Math.max(1, Number.parseInt(String(q.page ?? "1"), 10) || 1);
    const offset = (page - 1) * pageSize;

    const rows = await getCollection(pool, req.user!.id, {
      ...filters,
      sort,
      limit: pageSize,
      offset,
    });
    const stats = await getCollectionStats(pool, req.user!.id, filters);

    res.render("collection.njk", {
      rows: rows.map((row) => ({ ...row, price_display: priceDisplay(row) })),
      stats: { ...stats, total_value_display: formatCents(stats.total_value_cents) },
      filters: { ...filters, sort },
      page,
      pageSize,
      hasNext: rows.length === pageSize,
    });
  });

  return r;
}
