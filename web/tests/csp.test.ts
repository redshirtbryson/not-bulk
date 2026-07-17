import { describe, it, expect } from 'vitest';
import { createApp } from '../src/app.js';
import { FakePool, anonAgent, makeDeps } from './helpers.js';

const FINAL_CSP =
  "default-src 'self'; img-src 'self' http://127.0.0.1:9000; style-src 'self'; " +
  "script-src 'self' https://challenges.cloudflare.com; frame-ancestors 'none'";

describe('Content-Security-Policy', () => {
  it('serves the full final CSP (Turnstile host, frame-ancestors none, no inline)', async () => {
    const pool = new FakePool();
    const app = createApp(makeDeps({ pool }));
    const res = await anonAgent(app).get('/');
    expect(res.headers['content-security-policy']).toBe(FINAL_CSP);
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    expect(res.headers['content-security-policy']).not.toContain("'unsafe-inline'");
  });
});
