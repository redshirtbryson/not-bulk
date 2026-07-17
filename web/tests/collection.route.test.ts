import { describe, it, expect } from "vitest";
import request from "supertest";
import { createApp } from "../src/app.js";
import { FakePool, makeDeps } from "./helpers.js";

const AUTHED_USER = { id: "u1", email: "a@b.com", tier: "free" };
function withUser() {
  return (req: any, _res: any, next: any) => {
    req.user = AUTHED_USER;
    next();
  };
}

// Route issues getCollection() then getCollectionStats(): enqueue rows then the stats row.
function seed(pool: FakePool, rows: any[], stats: any) {
  pool.enqueue({ rows });
  pool.enqueue({ rows: [stats] });
}

const PRICED_ROW = {
  card_id: "c1", card_ref_id: "r1", crop_storage_key: "u1/b1/crops/c1.webp",
  name: "Charizard", set_name: "Base", number: "4", finish: "holofoil",
  quantity: 1, confidence: 0.98, status: "auto",
  price_cents: 1234, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z",
  batch_id: "b1", has_price_row: true,
};
const NO_DATA_ROW = { ...PRICED_ROW, card_id: "c2", name: "Pikachu", number: "58",
  price_cents: null, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z", has_price_row: true };
const PENDING_ROW = { ...PRICED_ROW, card_id: "c3", name: "Bulbasaur", number: "44",
  price_cents: null, price_source: null, price_fetched_at: null, has_price_row: false };
// Trailing-zero fixtures: (v/100)|round(2)|float in Nunjucks renders "$12.3" / "$12.0" for these —
// a naive template-arithmetic formatter would fail these two.
const WHOLE_DOLLAR_ROW = { ...PRICED_ROW, card_id: "c4", name: "Blastoise", number: "9",
  price_cents: 1200 };
const TRAILING_ZERO_ROW = { ...PRICED_ROW, card_id: "c5", name: "Venusaur", number: "15",
  price_cents: 1230 };

describe("GET /collection", () => {
  it("renders the grid, formats cents as $X.XX, and shows the stats bar", async () => {
    const pool = new FakePool();
    seed(pool, [PRICED_ROW, NO_DATA_ROW, PENDING_ROW], {
      total_cards: 3, total_value_cents: 1234, priced_fraction: 0.333, oldest_price_at: "2026-07-15T00:00:00.000Z",
    });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(200);
    expect(res.text).toContain("Charizard");
    expect(res.text).toContain("$12.34");        // 1234 cents formatted
    expect(res.text).toContain("no price data");  // price_cents NULL but has_price_row
    expect(res.text).toContain("pending price");  // no prices row at all
    expect(res.text).toContain("$12.34");         // stats total value
    expect(res.text).toContain("/img/crop/c1");   // grid thumb uses the user's crop
  });

  it("formats whole-dollar and trailing-zero cents correctly (never truncates the trailing zero)", async () => {
    const pool = new FakePool();
    seed(pool, [WHOLE_DOLLAR_ROW, TRAILING_ZERO_ROW], {
      total_cards: 2, total_value_cents: 2430, priced_fraction: 1, oldest_price_at: "2026-07-15T00:00:00.000Z",
    });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(200);
    expect(res.text).toContain("$12.00");   // 1200 cents — must NOT render "$12.0" or "$12"
    expect(res.text).toContain("$12.30");   // 1230 cents — must NOT render "$12.3"
    expect(res.text).toContain("$24.30");   // stats total value (2430 cents)
  });

  it("filters getCollection by user_id from the session (ownership, AC 7)", async () => {
    const pool = new FakePool();
    seed(pool, [], { total_cards: 0, total_value_cents: 0, priced_fraction: 0, oldest_price_at: null });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection");
    expect(pool.calls[0].params[0]).toBe("u1");
    expect(pool.calls[1].params[0]).toBe("u1");
  });

  it("passes sort/filter query params through to the query", async () => {
    const pool = new FakePool();
    seed(pool, [], { total_cards: 0, total_value_cents: 0, priced_fraction: 0, oldest_price_at: null });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection?sort=name_asc&set=base1&finish=holofoil&source=corrected&batch=b1&page=2");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/ORDER BY r\.name ASC/i);
    expect(params).toContain("base1");
    expect(params).toContain("holofoil");
    expect(params).toContain("b1");
    // page 2 with page_size 60 -> offset 60
    expect(params).toContain(60);
  });

  it("302 redirects anonymous users to /", async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await request(app).get("/collection");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
