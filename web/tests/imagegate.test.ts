import { describe, it, expect } from 'vitest';
import sharp from 'sharp';
import { gateImage } from '../src/services/imagegate.js';
import type { Config } from '../src/config.js';

const cfg = {
  quotas: { max_pixels: 50_000_000, max_photo_bytes: 10_485_760 },
} as unknown as Config;

// A solid-colour rectangle JPEG/PNG — structured, DCT-safe.
function solid(w: number, h: number, fmt: 'jpeg' | 'png', rgb = [30, 90, 200]) {
  return sharp({
    create: { width: w, height: h, channels: 3, background: { r: rgb[0], g: rgb[1], b: rgb[2] } },
  })[fmt]().toBuffer();
}

describe('gateImage — accepts', () => {
  it('accepts a valid JPEG and re-encodes to WebP', async () => {
    const res = await gateImage(await solid(200, 120, 'jpeg'), cfg);
    expect(res.ok).toBe(true);
    if (res.ok) {
      expect(res.width).toBe(200);
      expect(res.height).toBe(120);
      // WebP magic: 'RIFF'....'WEBP'
      expect(res.webp.subarray(0, 4).toString('latin1')).toBe('RIFF');
      expect(res.webp.subarray(8, 12).toString('latin1')).toBe('WEBP');
    }
  });

  it('accepts a valid PNG', async () => {
    const res = await gateImage(await solid(64, 64, 'png'), cfg);
    expect(res.ok).toBe(true);
  });

  it('applies EXIF orientation 6 then strips EXIF (dimensions swapped, no exif in output)', async () => {
    // Orientation 6 = rotate 90° CW on display. Author a 200x120 JPEG tagged
    // orientation 6; after .rotate() the pixel dimensions become 120x200.
    const tagged = await sharp({
      create: { width: 200, height: 120, channels: 3, background: { r: 200, g: 40, b: 40 } },
    })
      .withMetadata({ orientation: 6 })
      .jpeg()
      .toBuffer();

    const res = await gateImage(tagged, cfg);
    expect(res.ok).toBe(true);
    if (res.ok) {
      expect(res.width).toBe(120);
      expect(res.height).toBe(200);
      const meta = await sharp(res.webp).metadata();
      expect(meta.exif).toBeUndefined(); // re-encode stripped EXIF
      expect(meta.orientation).toBeUndefined();
    }
  });
});

describe('gateImage — rejects', () => {
  it("rejects a GIF on the magic-byte check with 'unsupported format'", async () => {
    // GIF header 'GIF89a' — sharp is never asked to decode it.
    const gif = Buffer.from('GIF89a', 'latin1');
    const res = await gateImage(gif, cfg);
    expect(res).toEqual({ ok: false, reason: 'unsupported format' });
  });

  it("rejects a text file with 'unsupported format'", async () => {
    const res = await gateImage(Buffer.from('this is not an image at all'), cfg);
    expect(res).toEqual({ ok: false, reason: 'unsupported format' });
  });

  it("rejects oversize bytes BEFORE decode with 'file too large'", async () => {
    // Valid JPEG magic bytes so the format check would pass; length exceeds the cap.
    const big = Buffer.alloc(cfg.quotas.max_photo_bytes + 1);
    big[0] = 0xff; big[1] = 0xd8; big[2] = 0xff;
    const res = await gateImage(big, cfg);
    expect(res).toEqual({ ok: false, reason: 'file too large' });
  });

  it("rejects a valid-header but corrupt JPEG body with 'corrupt image'", async () => {
    // JPEG magic bytes then garbage — passes format + size, fails sharp decode.
    const corrupt = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff]), Buffer.alloc(64, 0x7a)]);
    const res = await gateImage(corrupt, cfg);
    expect(res).toEqual({ ok: false, reason: 'corrupt image' });
  });

  it("rejects an over-pixel-cap image with 'image too large'", async () => {
    // 9000x6000 = 54 MP > 50 MP cap. A solid PNG is cheap to author (RLE-friendly).
    const bomb = await sharp({
      create: { width: 9000, height: 6000, channels: 3, background: { r: 10, g: 10, b: 10 } },
    })
      .png()
      .toBuffer();
    // Guard: keep the fixture under the byte cap so it reaches the pixel check,
    // not the size check (a solid 54 MP PNG compresses to well under 10 MB).
    expect(bomb.length).toBeLessThanOrEqual(cfg.quotas.max_photo_bytes);
    const res = await gateImage(bomb, cfg);
    expect(res).toEqual({ ok: false, reason: 'image too large' });
  });

  it('never throws out of gateImage on crafted malformed input (AC 8)', async () => {
    const junk = Buffer.from([0xff, 0xd8, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00]);
    await expect(gateImage(junk, cfg)).resolves.toMatchObject({ ok: false });
  });
});
