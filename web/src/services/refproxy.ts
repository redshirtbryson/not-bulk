import sharp from "sharp";
import type { Pool } from "pg";
import type { Config } from "../config.js";
import type { Storage } from "./storage.js";

/**
 * Reference-image proxy. NOT an SSRF surface: it fetches ONLY card_refs.image_url —
 * trusted reference data populated from pokemontcg.io at index-build time, never user
 * input — and only when the parsed hostname is EXACTLY cfg.refproxy.allowed_image_host
 * ("images.pokemontcg.io"). The user-URL fetcher with the full SSRF gate is M2's
 * worker-side code. A WHATWG-URL parse + exact single-host equality check is the complete
 * control here. On ANY failure the function returns null and the caller renders a placeholder.
 */
export async function ensureRefCached(
  pool: Pool,
  storage: Storage,
  cfg: Config,
  cardRefId: string,
): Promise<string | null> {
  if (!cardRefId) return null;
  try {
    const { rows } = await pool.query(
      `SELECT ref_cached_key, image_url FROM card_refs WHERE id = $1`,
      [cardRefId],
    );
    const ref = rows[0];
    if (!ref) return null;
    if (ref.ref_cached_key) return ref.ref_cached_key as string;
    if (!ref.image_url) return null;

    // WHATWG parse + exact-host check (the complete control — see header comment).
    let url: URL;
    try {
      url = new URL(ref.image_url as string);
    } catch {
      return null;
    }
    if (url.protocol !== "https:") return null;
    if (url.hostname !== cfg.refproxy.allowed_image_host) return null;

    // redirect:"error" — a redirect off the pinned host would defeat the host check.
    const resp = await fetch(url, { redirect: "error" });
    if (!resp.ok) return null;

    const ctype = resp.headers.get("content-type") ?? "";
    if (!ctype.startsWith("image/")) return null;

    // Content-Length cap (fast-fail when the header is present).
    const declared = Number(resp.headers.get("content-length") ?? "0");
    if (declared && declared > cfg.refproxy.max_bytes) return null;

    // Hard bound: stream the body and abort the moment cumulative bytes exceed
    // max_bytes. A chunked / no-Content-Length response must NOT be fully
    // materialized before the cap rejects it (OOM protection is load-bearing).
    if (!resp.body) return null;
    const chunks: Buffer[] = [];
    let total = 0;
    for await (const chunk of resp.body as unknown as AsyncIterable<Uint8Array>) {
      total += chunk.length;
      if (total > cfg.refproxy.max_bytes) return null; // abort before OOM
      chunks.push(Buffer.from(chunk));
    }
    const raw = Buffer.concat(chunks);

    const webp = await sharp(raw).webp().toBuffer();
    const key = `${cfg.refproxy.cache_prefix}/${cardRefId}.webp`;
    await storage.put(key, webp, "image/webp");
    await pool.query(
      `UPDATE card_refs SET ref_cached_key = $1 WHERE id = $2 RETURNING id`,
      [key, cardRefId],
    );
    return key;
  } catch {
    return null;
  }
}
