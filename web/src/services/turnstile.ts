import type { Config } from "../config.js";

const SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const BYPASS = process.env.DEV_BYPASS_TURNSTILE === "1";

if (BYPASS) {
  // Logged ONCE at module init, loudly. Dev only.
  console.warn("TURNSTILE BYPASSED — dev only");
}

export async function verifyTurnstile(
  cfg: Config,
  token: string,
  ip: string | undefined,
): Promise<boolean> {
  if (BYPASS) return true;
  if (!token) return false;

  const body = new URLSearchParams();
  body.set("secret", cfg.turnstile.secret);
  body.set("response", token);
  if (ip) body.set("remoteip", ip);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(SITEVERIFY_URL, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
      signal: controller.signal,
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { success?: boolean };
    return data.success === true;
  } catch {
    return false; // network error / timeout / abort
  } finally {
    clearTimeout(timer);
  }
}
