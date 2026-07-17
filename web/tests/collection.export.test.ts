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

const HEADER = "name,set,number,finish,quantity,price,price_source,price_date,confidence,batch,image_filename";

const PRICED = {
  card_id: "c1", card_ref_id: "r1", crop_storage_key: "u1/b1/crops/c1.webp",
  name: "Charizard", set_name: "Base", number: "4", finish: "holofoil",
  quantity: 2, confidence: 0.98, status: "auto",
  price_cents: 1234, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z",
  batch_id: "b1", has_price_row: true,
};
const NULL_PRICE = { ...PRICED, card_id: "c2", name: "Pikachu", number: "58",
  price_cents: null, price_source: "pokemontcg", price_fetched_at: "2026-07-15T00:00:00.000Z" };
const COMMA_NAME = { ...PRICED, card_id: "c3", name: "Mr. Mime, Prime", number: "63",
  price_cents: 500 };

describe("GET /collection/export.csv", () => {
  it("streams the exact header + rows, formats price, empty cell for null, quotes commas", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [PRICED, NULL_PRICE, COMMA_NAME] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection/export.csv");

    expect(res.status).toBe(200);
    expect(res.headers["content-type"]).toMatch(/text\/csv/);
    expect(res.headers["content-disposition"]).toBe('attachment; filename="notbulk-collection.csv"');

    const lines = res.text.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");
    expect(lines[0]).toBe(HEADER);
    // priced row: $12.34, image basename c1.webp
    expect(lines[1]).toBe("Charizard,Base,4,holofoil,2,$12.34,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c1.webp");
    // null price -> empty cell (NOT $0.00), between quantity and price_source
    expect(lines[2]).toBe("Pikachu,Base,58,holofoil,2,,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c2.webp");
    // comma in name -> whole field quoted (RFC 4180)
    expect(lines[3]).toBe('"Mr. Mime, Prime",Base,63,holofoil,2,$5.00,pokemontcg,2026-07-15T00:00:00.000Z,0.98,b1,c3.webp');
  });

  it("null price cell is empty string, never $0.00", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [NULL_PRICE] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    const res = await request(app).get("/collection/export.csv");
    expect(res.text).not.toContain("$0.00");
    const cells = res.text.replace(/\r\n/g, "\n").split("\n")[1].split(",");
    expect(cells[5]).toBe(""); // price column empty
  });

  it("filters the export query by user_id (ownership, AC 7)", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    const app = createApp(makeDeps({ pool, sessionMiddleware: withUser() }));
    await request(app).get("/collection/export.csv");
    expect(pool.calls[0].params[0]).toBe("u1");
    expect(pool.calls[0].sql).not.toMatch(/LIMIT/i); // export is the full collection
  });

  it("302 redirects anonymous users to /", async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await request(app).get("/collection/export.csv");
    expect(res.status).toBe(302);
    expect(res.headers.location).toBe("/");
  });
});
