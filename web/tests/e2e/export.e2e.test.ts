// M4 acceptance gate: async PDF export loop + HEIC upload leg, against REAL local
// Postgres (5434) + MinIO (9000), with a REAL notbulk-export-worker subprocess.
// Gated on E2E=1. The export worker's browser is stubbed via NOTBULK_STUB_PDF=1 so
// no Chromium is needed in CI; the REAL render is covered by the separate PDF_RENDER
// leg (Step 6). Self-cleaning in afterAll.
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { Pool } from "pg";
import { uuidv7 } from "uuidv7";
import { createHash } from "node:crypto";
import request from "supertest";
import sharp from "sharp";
import { createApp } from "../../src/app.js";
import { loadConfig } from "../../src/config.js";
import { getPool } from "../../src/db.js";
import { Storage } from "../../src/services/storage.js";
import { sessionMiddleware } from "../../src/middleware/session.js";

const RUN = process.env.E2E === "1";
const d = RUN ? describe : describe.skip;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REF_ID = "e2e-export-base1-4";

async function waitFor<T>(fn: () => Promise<T | null>, timeoutMs: number): Promise<T> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const v = await fn();
    if (v) return v;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("waitFor timed out");
}

d("M4 e2e export loop + HEIC (real Postgres + MinIO + export worker)", () => {
  let pool: Pool;
  let worker: ChildProcess;
  let userId: string;
  let token: string;
  let batchId: string;
  let cropKey: string;
  const cleanupBatches: string[] = [];

  beforeAll(async () => {
    pool = getPool();
    const cfg = loadConfig();
    const storage = new Storage(cfg);

    await pool.query(
      `INSERT INTO card_refs (id, name, set_id, set_name, number, image_url, finishes)
         VALUES ($1,'E2E Export Card','e2e-set','E2E Set','4','https://example.invalid/c.png', ARRAY['holofoil'])
       ON CONFLICT (id) DO NOTHING`,
      [REF_ID],
    );

    userId = uuidv7();
    await pool.query(`INSERT INTO users (id, email, tier) VALUES ($1,$2,'free')`, [
      userId, `e2e-export-${userId}@test.local`,
    ]);
    const raw = uuidv7();
    token = raw;
    await pool.query(
      `INSERT INTO sessions (id, user_id, token_hash, expires_at)
         VALUES ($1,$2,$3, now() + interval '30 days')`,
      [uuidv7(), userId, createHash("sha256").update(raw).digest("hex")],
    );

    // Seed a small collection: one batch, one photo, one auto card with a crop in MinIO + a price.
    batchId = uuidv7();
    cleanupBatches.push(batchId);
    await pool.query(
      `INSERT INTO batches (id, user_id, status) VALUES ($1,$2,'complete')`,
      [batchId, userId],
    );
    const photoId = uuidv7();
    await pool.query(
      `INSERT INTO photos (id, batch_id, status, source_type, storage_key)
         VALUES ($1,$2,'done','upload',$3)`,
      [photoId, batchId, `${userId}/${batchId}/${photoId}.webp`],
    );
    const cardId = uuidv7();
    cropKey = `${userId}/${batchId}/crops/${cardId}.webp`;
    // A real WebP crop so storage.get -> data URI works end to end.
    const cropBuf = await sharp({ create: { width: 32, height: 44, channels: 3, background: "#c33" } })
      .webp().toBuffer();
    await storage.put(cropKey, cropBuf, "image/webp");
    await pool.query(
      `INSERT INTO cards (id, photo_id, crop_index, card_ref_id, crop_storage_key, finish, quantity, confidence, status)
         VALUES ($1,$2,0,$3,$4,'holofoil',1,99,'auto')`,
      [cardId, photoId, REF_ID, cropKey],
    );
    await pool.query(
      `INSERT INTO prices (card_ref_id, finish, price_cents, source, fetched_at)
         VALUES ($1,'holofoil',1234,'pokemontcg', now())
       ON CONFLICT (card_ref_id, finish) DO UPDATE SET price_cents=EXCLUDED.price_cents`,
      [REF_ID],
    );

    // Spawn the REAL export worker with the browser stubbed.
    worker = spawn("pnpm", ["export-worker"], {
      cwd: path.resolve(__dirname, "../../"),
      env: { ...process.env, NOTBULK_STUB_PDF: "1" },
      stdio: "inherit",
    });
    await new Promise((r) => setTimeout(r, 2000));
  }, 30_000);

  afterAll(async () => {
    if (worker) worker.kill("SIGTERM");
    const cfg = loadConfig();
    const storage = new Storage(cfg);
    // Remove export artifacts + crops for the seeded user.
    const exps = await pool.query(`SELECT storage_key FROM exports WHERE user_id=$1`, [userId]);
    for (const e of exps.rows) if (e.storage_key) await storage.delete(e.storage_key).catch(() => {});
    await storage.delete(cropKey).catch(() => {});
    for (const b of cleanupBatches) {
      const photos = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [b]);
      const cards = await pool.query(
        `SELECT crop_storage_key FROM cards c JOIN photos p ON p.id=c.photo_id WHERE p.batch_id=$1`, [b]);
      for (const p of photos.rows) if (p.storage_key) await storage.delete(p.storage_key).catch(() => {});
      for (const c of cards.rows) if (c.crop_storage_key) await storage.delete(c.crop_storage_key).catch(() => {});
    }
    await pool.query(`DELETE FROM prices WHERE card_ref_id=$1`, [REF_ID]);
    await pool.query(`DELETE FROM users WHERE id=$1`, [userId]); // cascades sessions/batches/photos/cards/jobs/exports
    await pool.query(`DELETE FROM card_refs WHERE id=$1`, [REF_ID]);
    await pool.end();
  });

  it("POST export -> worker renders -> row ready -> signed download returns a PDF", async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool, cfg, storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    const post = await request(app).post("/collection/export.pdf").set("Cookie", `nb_session=${token}`);
    expect(post.status).toBe(302);
    const exportId = post.headers.location.split("/").pop()!;

    // Worker drains the 'export' job and marks the row ready.
    await waitFor(async () => {
      const r = await pool.query(`SELECT status FROM exports WHERE id=$1`, [exportId]);
      return r.rows[0]?.status === "ready" ? true : null;
    }, 60_000);

    const download = await request(app)
      .get(`/collection/exports/${exportId}/download`)
      .set("Cookie", `nb_session=${token}`);
    expect(download.status).toBe(302);
    const signed = download.headers.location;
    expect(signed).toContain("127.0.0.1:9000");

    const pdf = await fetch(signed);
    expect(pdf.ok).toBe(true);
    const bytes = Buffer.from(await pdf.arrayBuffer());
    expect(bytes.subarray(0, 4).toString("latin1")).toBe("%PDF");
  }, 120_000);

  it("HEIC upload is accepted (not a 400) and stores a WebP photo", async () => {
    const cfg = loadConfig();
    const app = createApp({
      pool, cfg, storage: new Storage(cfg),
      mailer: { sendMagicLink: async () => {} },
      sessionMiddleware: sessionMiddleware(pool, cfg),
    });

    // Use the REAL committed HEIC fixture — sharp cannot WRITE heif here (and its
    // bundled libheif cannot DECODE HEVC either); the upload gate decodes it via
    // heic-convert. The fixture is a genuine heic-branded, decodable image.
    const heic = readFileSync(
      path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "fixtures", "sample-card.heic"),
    );
    expect(heic.subarray(4, 8).toString("latin1")).toBe("ftyp"); // sanity: real HEIC

    const create = await request(app)
      .post("/batches")
      .set("Cookie", `nb_session=${token}`)
      .attach("photos", heic, { filename: "card.heic", contentType: "image/heic" });
    expect(create.status).toBe(302); // accepted, not 400
    const heicBatchId = create.headers.location.split("/").pop()!;
    cleanupBatches.push(heicBatchId);

    // The upload gate re-encodes to WebP before the queue: the photo lands stored as .webp.
    const photo = await waitFor(async () => {
      const r = await pool.query(`SELECT storage_key FROM photos WHERE batch_id=$1`, [heicBatchId]);
      return r.rows[0]?.storage_key ? r.rows[0] : null;
    }, 30_000);
    expect(photo.storage_key).toMatch(/\.webp$/);
    const stored = await new Storage(cfg).get(photo.storage_key);
    // WebP magic: "RIFF"...."WEBP".
    expect(stored.subarray(0, 4).toString("latin1")).toBe("RIFF");
    expect(stored.subarray(8, 12).toString("latin1")).toBe("WEBP");
  }, 60_000);
});

const RENDER = process.env.PDF_RENDER === "1";
const dr = RENDER ? describe : describe.skip;

dr("renderCollectionPdf (REAL Puppeteer, no stub)", () => {
  it("produces a real PDF buffer from one card", async () => {
    // Import lazily so the stub-only E2E never touches the browser module.
    const { renderCollectionPdf } = await import("../../src/lib/pdf.js");
    const { loadConfig } = await import("../../src/config.js");
    const cfg = loadConfig();
    const png1x1 =
      "data:image/webp;base64,UklGRhIAAABXRUJQVlA4TAYAAAAvAAAAAAfQ//73v/+BiOh/AAA=";
    const buf = await renderCollectionPdf(
      [{ cropDataUri: png1x1, name: "Pikachu", set: "Base", number: "58",
         finish: "holofoil", priceDisplay: "$12.34", quantity: 1 }],
      { totalCards: 1, totalValueDisplay: "$12.34", generatedAt: new Date().toISOString() },
      cfg,
    );
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString("latin1")).toBe("%PDF");
    expect(buf.byteLength).toBeGreaterThan(1000); // a real render, not the 60-byte stub
  }, 60_000);
});
