import { describe, it, expect } from 'vitest';
import { Storage } from '../src/services/storage.js';
import { loadConfig } from '../src/config.js';

const run = process.env.STORAGE_INTEGRATION === '1';

describe.skipIf(!run)('Storage ↔ real MinIO round-trip', () => {
  it('put → signedGetUrl → fetch bytes back → delete', async () => {
    const cfg = loadConfig();
    const s = new Storage(cfg);
    const key = `itest/${Date.now()}.webp`;
    const body = Buffer.from([0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4]); // arbitrary bytes

    await s.put(key, body, 'image/webp');
    const url = await s.signedGetUrl(key);
    const resp = await fetch(url);
    expect(resp.status).toBe(200);
    const got = Buffer.from(await resp.arrayBuffer());
    expect(got.equals(body)).toBe(true);
    await s.delete(key);
  });
});
