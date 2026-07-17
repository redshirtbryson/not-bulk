import sharp from 'sharp';
import type { Config } from '../config.js';

export interface GateResult {
  ok: true;
  webp: Buffer;
  width: number;
  height: number;
}
export interface GateReject {
  ok: false;
  reason: string;
}

const JPEG_MAGIC = Buffer.from([0xff, 0xd8, 0xff]);
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function hasMagic(bytes: Buffer): boolean {
  if (bytes.length >= 3 && bytes.subarray(0, 3).equals(JPEG_MAGIC)) return true;
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(PNG_MAGIC)) return true;
  return false;
}

export async function gateImage(
  bytes: Buffer,
  cfg: Config,
): Promise<GateResult | GateReject> {
  // 1. Byte-length cap FIRST — cheapest, before any decode work.
  if (bytes.length > cfg.quotas.max_photo_bytes) {
    return { ok: false, reason: 'file too large' };
  }

  // 2. Magic bytes — JPEG/PNG only in M2 (HEIC is M4). Reject anything else
  //    before sharp ever touches the buffer.
  if (!hasMagic(bytes)) {
    return { ok: false, reason: 'unsupported format' };
  }

  // 3. Decode + re-encode. limitInputPixels enforces the pixel cap (sharp
  //    throws when exceeded). .rotate() applies EXIF orientation; the WebP
  //    re-encode strips all metadata (EXIF/orientation gone). failOn:'error'
  //    makes truncated/corrupt inputs throw rather than silently pass.
  try {
    const pipeline = sharp(bytes, {
      limitInputPixels: cfg.quotas.max_pixels,
      failOn: 'error',
    })
      .rotate()
      .webp({ quality: 75 });

    const { data, info } = await pipeline.toBuffer({ resolveWithObject: true });
    return { ok: true, webp: data, width: info.width, height: info.height };
  } catch (err) {
    // sharp's pixel-limit error message contains "pixels"; distinguish it
    // from a generic decode failure so callers/tests get a precise reason.
    const msg = err instanceof Error ? err.message.toLowerCase() : '';
    if (msg.includes('pixel')) {
      return { ok: false, reason: 'image too large' };
    }
    return { ok: false, reason: 'corrupt image' };
  }
}
