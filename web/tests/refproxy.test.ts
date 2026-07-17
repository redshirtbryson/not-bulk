import { describe, it, expect, vi, afterEach } from "vitest";
import sharp from "sharp";
import { ensureRefCached } from "../src/services/refproxy.js";
import { FakePool, FakeStorage, testCfg } from "./helpers.js";
import type { Config } from "../src/config.js";
import type { Pool } from "pg";
import type { Storage } from "../src/services/storage.js";

// A tiny real PNG so sharp() has something valid to transcode on the first-fetch path.
async function tinyPng(): Promise<Buffer> {
  return sharp({
    create: { width: 4, height: 4, channels: 3, background: { r: 10, g: 20, b: 30 } },
  })
    .png()
    .toBuffer();
}

// Build a Response-like object for the mocked global fetch: ok, headers.get, and a
// streaming async-iterable `body` (mirrors undici's web ReadableStream). `body` is what
// ensureRefCached now iterates for the hard byte cap; `chunkSize` splits the payload into
// multiple chunks so the streaming abort path is exercised. Pass declaredLength=null to
// simulate a chunked / no-Content-Length response.
function fetchResponse(
  body: Buffer,
  contentType = "image/png",
  { ok = true, status = 200, chunkSize = body.length, declaredLength = body.length as number | null } = {},
) {
  async function* iter() {
    for (let i = 0; i < body.length; i += Math.max(1, chunkSize)) {
      yield new Uint8Array(body.subarray(i, i + Math.max(1, chunkSize)));
    }
  }
  return {
    ok,
    status,
    headers: {
      get: (h: string) =>
        h.toLowerCase() === "content-type"
          ? contentType
          : h.toLowerCase() === "content-length"
            ? declaredLength === null
              ? null
              : String(declaredLength)
            : null,
    },
    body: iter(),
  } as unknown as Response;
}

afterEach(() => vi.restoreAllMocks());

describe("ensureRefCached", () => {
  it("returns null for a null/empty cardRefId without touching the pool", async () => {
    const pool = new FakePool();
    const storage = new FakeStorage();
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "");
    expect(key).toBeNull();
    expect(pool.calls.length).toBe(0);
  });

  it("cached-key path: returns the existing ref_cached_key, never fetches", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: "refs/ref-1.webp", image_url: "https://images.pokemontcg.io/x/1.png" }] });
    const storage = new FakeStorage();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBe("refs/ref-1.webp");
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(storage.puts.length).toBe(0);
  });

  it("first-fetch path: fetches, transcodes to webp, puts, updates card_refs, returns key", async () => {
    const png = await tinyPng();
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] }); // SELECT
    pool.enqueue({ rows: [{ id: "ref-1" }] }); // UPDATE ... RETURNING
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(fetchResponse(png, "image/png"));

    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");

    expect(key).toBe("refs/ref-1.webp");
    // put'd a webp under the inline refs/{id}.webp key
    expect(storage.puts.length).toBe(1);
    expect(storage.puts[0].key).toBe("refs/ref-1.webp");
    expect(storage.puts[0].contentType).toBe("image/webp");
    expect(storage.puts[0].body.slice(0, 4).toString("latin1")).toBe("RIFF"); // WEBP container magic
    // card_refs updated with the cached key
    const update = pool.calls[1];
    expect(update.sql).toMatch(/UPDATE card_refs SET ref_cached_key/i);
    expect(update.params).toEqual(["refs/ref-1.webp", "ref-1"]);
  });

  it("non-allowlisted image_url host: returns null, never fetches", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://evil.example.com/x.png" }] });
    const storage = new FakeStorage();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(storage.puts.length).toBe(0);
  });

  it("fetch failure (non-ok / transport throw): returns null, no put, no update", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("boom"));
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0);
    expect(pool.calls.length).toBe(1); // only the SELECT ran
  });

  it("non-image content-type: returns null, no put", async () => {
    const png = await tinyPng();
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(fetchResponse(png, "text/html"));
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, testCfg, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0);
  });

  it("oversize via Content-Length: returns null before streaming, no put", async () => {
    const png = await tinyPng();
    const smallCap = { ...testCfg, refproxy: { ...testCfg.refproxy, max_bytes: 8 } } as Config;
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    // Declared length exceeds the cap → fast-fail before any body read.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(fetchResponse(png, "image/png", { declaredLength: 999999 }));
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, smallCap, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0);
  });

  it("oversize with NO Content-Length: aborts mid-stream, returns null, never buffers/puts", async () => {
    // 4 KiB payload, delivered in 512 B chunks, no declared Content-Length; cap is 1 KiB.
    // The streaming cap must abort after the cumulative total crosses max_bytes — proving
    // the body is NOT fully materialized before rejection (the OOM protection is real).
    const big = Buffer.alloc(4096, 0xab);
    const smallCap = { ...testCfg, refproxy: { ...testCfg.refproxy, max_bytes: 1024 } } as Config;
    const pool = new FakePool();
    pool.enqueue({ rows: [{ ref_cached_key: null, image_url: "https://images.pokemontcg.io/base/4.png" }] });
    const storage = new FakeStorage();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      fetchResponse(big, "image/png", { chunkSize: 512, declaredLength: null }),
    );
    const key = await ensureRefCached(pool as unknown as Pool, storage as unknown as Storage, smallCap, "ref-1");
    expect(key).toBeNull();
    expect(storage.puts.length).toBe(0); // aborted before sharp/put
    expect(pool.calls.length).toBe(1); // only the SELECT ran (no UPDATE)
  });
});
