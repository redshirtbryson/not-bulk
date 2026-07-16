// Placeholder Turnstile verification function. A later task provides the
// real Cloudflare Turnstile server-side verification call; app.ts only
// needs the type for its optional DI seam today.
export async function verifyTurnstile(_token: string): Promise<boolean> {
  throw new Error("verifyTurnstile is not implemented yet; a later task wires the real Turnstile check");
}
