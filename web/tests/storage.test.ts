import { describe, it, expect, vi } from 'vitest';
import { Storage } from '../src/services/storage.js';
import type { Config } from '../src/config.js';

const cfg = {
  storage: {
    endpoint: 'http://127.0.0.1:9000',
    bucket: 'notbulk',
    access_key: 'minioadmin',
    secret_key: 'minioadmin',
    signed_url_ttl_seconds: 900,
  },
} as unknown as Config;

describe('Storage key formats', () => {
  it('photoKey is {userId}/{batchId}/{photoId}.webp', () => {
    const s = new Storage(cfg);
    expect(s.photoKey('u1', 'b1', 'p1')).toBe('u1/b1/p1.webp');
  });

  it('cropKey is {userId}/{batchId}/crops/{cardId}.webp', () => {
    const s = new Storage(cfg);
    expect(s.cropKey('u1', 'b1', 'c1')).toBe('u1/b1/crops/c1.webp');
  });
});

describe('Storage.put', () => {
  it('sends a PutObjectCommand with bucket, key, body, ContentType', async () => {
    const s = new Storage(cfg);
    const send = vi.fn().mockResolvedValue({});
    // Reach into the private client for a unit boundary — we do NOT hit S3.
    (s as unknown as { client: { send: typeof send } }).client = { send };
    await s.put('u1/b1/p1.webp', Buffer.from('abc'), 'image/webp');
    expect(send).toHaveBeenCalledOnce();
    const cmd = send.mock.calls[0][0];
    expect(cmd.input).toMatchObject({
      Bucket: 'notbulk',
      Key: 'u1/b1/p1.webp',
      ContentType: 'image/webp',
    });
    expect(Buffer.isBuffer(cmd.input.Body)).toBe(true);
  });
});
