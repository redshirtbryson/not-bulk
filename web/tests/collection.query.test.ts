import { describe, it, expect } from "vitest";
import { getCollection, getCollectionStats } from "../src/queries/collection.js";
import { FakePool } from "./helpers.js";
import type { Pool } from "pg";

describe("getCollection SQL shape + params", () => {
  it("owner-scoped, no filters, value_desc: binds user_id, status whitelist, LIMIT/OFFSET; no interpolation", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { sort: "value_desc", limit: 60, offset: 0 });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/FROM cards c/i);
    expect(sql).toMatch(/JOIN photos p ON c\.photo_id\s*=\s*p\.id/i);
    expect(sql).toMatch(/JOIN batches b ON p\.batch_id\s*=\s*b\.id/i);
    expect(sql).toMatch(/JOIN card_refs r ON c\.card_ref_id\s*=\s*r\.id/i);
    expect(sql).toMatch(/LEFT JOIN prices pr ON pr\.card_ref_id\s*=\s*c\.card_ref_id AND pr\.finish\s*=\s*c\.finish/i);
    expect(sql).toMatch(/WHERE b\.user_id\s*=\s*\$1/i);
    expect(sql).toMatch(/c\.status IN \('auto','validated','corrected'\)/i);
    expect(sql).toMatch(/\(pr\.card_ref_id IS NOT NULL\) AS has_price_row/i);
    expect(sql).toMatch(/ORDER BY pr\.price_cents \* c\.quantity DESC NULLS LAST/i);
    expect(sql).toMatch(/LIMIT \$2 OFFSET \$3/i);
    expect(params).toEqual(["u1", 60, 0]);
    // no value ever concatenated into the SQL text
    expect(sql).not.toContain("u1");
  });

  it("all filters + name_asc: appends bound fragments in order, ORDER BY r.name ASC", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", {
      batchId: "b1", set: "base1", finish: "holofoil", source: "corrected", sort: "name_asc", limit: 60, offset: 60,
    });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/AND b\.id\s*=\s*\$2/i);
    expect(sql).toMatch(/AND r\.set_id\s*=\s*\$3/i);
    expect(sql).toMatch(/AND c\.finish\s*=\s*\$4/i);
    // source 'corrected' -> status IN ('validated','corrected')
    expect(sql).toMatch(/AND c\.status IN \('validated','corrected'\)/i);
    expect(sql).toMatch(/ORDER BY r\.name ASC/i);
    expect(sql).toMatch(/LIMIT \$5 OFFSET \$6/i);
    expect(params).toEqual(["u1", "b1", "base1", "holofoil", 60, 60]);
  });

  it("source 'auto' -> status='auto'; set_asc -> ORDER BY r.set_name, r.number", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { source: "auto", sort: "set_asc", limit: 60, offset: 0 });
    const { sql } = pool.calls[0];
    expect(sql).toMatch(/AND c\.status\s*=\s*'auto'/i);
    expect(sql).toMatch(/ORDER BY r\.set_name, r\.number/i);
  });

  it("unknown sort falls back to value_desc (whitelist, never interpolated)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    await getCollection(pool as unknown as Pool, "u1", { sort: "; DROP TABLE cards;--", limit: 60, offset: 0 });
    const { sql } = pool.calls[0];
    expect(sql).toMatch(/ORDER BY pr\.price_cents \* c\.quantity DESC NULLS LAST/i);
    expect(sql).not.toContain("DROP TABLE");
  });
});

describe("getCollectionStats", () => {
  it("owner-scoped aggregate: sums, priced fraction, oldest price; binds user_id", async () => {
    const pool = new FakePool();
    pool.enqueue({
      rows: [{ total_cards: 12, total_value_cents: 45600, priced_fraction: 0.75, oldest_price_at: "2026-07-15T00:00:00.000Z" }],
    });
    const stats = await getCollectionStats(pool as unknown as Pool, "u1", {});
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/COALESCE\(SUM\(c\.quantity\), 0\)/i);
    expect(sql).toMatch(/COALESCE\(SUM\(COALESCE\(pr\.price_cents, 0\) \* c\.quantity\), 0\)/i);
    expect(sql).toMatch(/MIN\(pr\.fetched_at\)/i);
    expect(sql).toMatch(/WHERE b\.user_id\s*=\s*\$1/i);
    expect(params).toEqual(["u1"]);
    expect(stats).toEqual({ total_cards: 12, total_value_cents: 45600, priced_fraction: 0.75, oldest_price_at: "2026-07-15T00:00:00.000Z" });
  });

  it("coerces numeric aggregates from pg strings (SUM/COUNT come back as text)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ total_cards: "12", total_value_cents: "45600", priced_fraction: "0.75", oldest_price_at: null }] });
    const stats = await getCollectionStats(pool as unknown as Pool, "u1", {});
    expect(stats.total_cards).toBe(12);
    expect(stats.total_value_cents).toBe(45600);
    expect(stats.priced_fraction).toBeCloseTo(0.75);
    expect(stats.oldest_price_at).toBeNull();
  });
});
