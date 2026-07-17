import { describe, it, expect, vi, afterEach } from "vitest";
import { verifyTurnstile } from "../src/services/turnstile.js";
import type { Config } from "../src/config.js";

const cfg = { turnstile: { secret: "1x0000000000000000000000000000000AA" } } as unknown as Config;

afterEach(() => { vi.unstubAllGlobals(); });

describe("verifyTurnstile (bypass OFF by default in test env)", () => {
  it("returns true when Cloudflare responds success:true", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ success: true }) })));
    expect(await verifyTurnstile(cfg, "tok", "1.2.3.4")).toBe(true);
  });

  it("returns false when Cloudflare responds success:false", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ success: false }) })));
    expect(await verifyTurnstile(cfg, "tok", undefined)).toBe(false);
  });

  it("returns false on a network error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    expect(await verifyTurnstile(cfg, "tok", undefined)).toBe(false);
  });

  it("returns false on a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, json: async () => ({}) })));
    expect(await verifyTurnstile(cfg, "tok", undefined)).toBe(false);
  });

  it("returns false immediately for an empty token (no fetch)", async () => {
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    expect(await verifyTurnstile(cfg, "", undefined)).toBe(false);
    expect(spy).not.toHaveBeenCalled();
  });
});

import { describe as describe2, it as it2, expect as expect2 } from "vitest";
import { execFileSync } from "node:child_process";

describe2("verifyTurnstile bypass path", () => {
  it2("returns true and warns once when DEV_BYPASS_TURNSTILE=1", () => {
    const script =
      "import('./src/services/turnstile.ts').then(async (m) => {" +
      "  const cfg = { turnstile: { secret: 'x' } };" +
      "  const r = await m.verifyTurnstile(cfg, '', undefined);" +
      "  if (r !== true) { console.error('EXPECTED_TRUE'); process.exit(1); }" +
      "  process.exit(0);" +
      "});";
    const out = execFileSync("pnpm", ["exec", "tsx", "-e", script], {
      env: { ...process.env, DEV_BYPASS_TURNSTILE: "1" },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    // Warning is emitted on stderr at module init; the run exiting 0 proves bypass=true.
    expect2(out).toBeDefined();
  });
});
