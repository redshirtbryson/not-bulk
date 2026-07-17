import sharp from 'sharp';
// heic-convert has no ESM default-export types in some versions; import as CJS interop.
import heicConvert from 'heic-convert';
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

export type ImageFormat = 'jpeg' | 'png' | 'heif';

const JPEG_MAGIC = Buffer.from([0xff, 0xd8, 0xff]);
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

// ISO-BMFF HEIF brands (bytes[8..12] of an `ftyp` box). heic-convert decodes
// all of these. `avif` is deliberately NOT here — AVIF is a different codec path
// and out of scope for M4's HEIC support.
const HEIF_BRANDS = new Set([
  'heic', 'heix', 'hevc', 'hevx', 'mif1', 'msf1', 'heim', 'heis', 'hevm', 'hevs',
]);

/**
 * Sniff the container format from magic bytes only — no decode, no config.
 * Returns the format tag or null. HEIF detection: an ISO-BMFF `ftyp` box
 * (bytes[4..8] === 'ftyp') whose major brand (bytes[8..12]) is a known HEIF brand.
 */
export function isSupportedImage(bytes: Buffer): ImageFormat | null {
  if (bytes.length >= 3 && bytes.subarray(0, 3).equals(JPEG_MAGIC)) return 'jpeg';
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(PNG_MAGIC)) return 'png';
  if (
    bytes.length >= 12 &&
    bytes.subarray(4, 8).toString('latin1') === 'ftyp' &&
    HEIF_BRANDS.has(bytes.subarray(8, 12).toString('latin1'))
  ) {
    return 'heif';
  }
  return null;
}

export async function gateImage(
  bytes: Buffer,
  cfg: Config,
): Promise<GateResult | GateReject> {
  // 1. Byte-length cap FIRST — cheapest, before any decode work.
  if (bytes.length > cfg.quotas.max_photo_bytes) {
    return { ok: false, reason: 'file too large' };
  }

  // 2. Magic-byte sniff → format tag. Unknown format is rejected before sharp
  //    touches the buffer. HEIF is accepted only when the config kill-switch is on;
  //    when off, HEIF is treated like any other unsupported format (never decoded).
  const format = isSupportedImage(bytes);
  if (format === null) {
    return { ok: false, reason: 'unsupported format' };
  }
  if (format === 'heif' && !cfg.upload.accept_heic) {
    return { ok: false, reason: 'unsupported format' };
  }

  // 3. Decode + re-encode. sharp decodes JPEG/PNG natively; HEIC (HEVC) is decoded
  //    FIRST by heic-convert (sharp's bundled libheif has no HEVC decoder) into a JPEG
  //    buffer, which then feeds the SAME sharp pipeline. Either decoder throwing (a
  //    truncated/corrupt input) is caught below → GateReject. limitInputPixels enforces
  //    the pixel cap; .rotate() applies EXIF orientation; the WebP re-encode strips all
  //    metadata (nothing raw is stored). AC-8: never throws out of gateImage.
  try {
    let sharpInput = bytes;
    if (format === 'heif') {
      // WASM HEVC decode → JPEG. Throws on a malformed HEIC (caught below).
      // @types/heic-convert declares `buffer: ArrayBufferLike`, but the runtime
      // (heic-decode) actually iterates the value as a Uint8Array/Buffer — passing
      // a sliced-out plain ArrayBuffer breaks at runtime ("Spread syntax requires
      // ...iterable[Symbol.iterator]"). The types are simply wrong here; a Node
      // Buffer (itself a Uint8Array) is what the library needs, so we deliberately
      // widen the type at the call site rather than changing the value passed.
      const jpeg = await heicConvert({
        buffer: bytes as unknown as ArrayBufferLike,
        format: 'JPEG',
        quality: 0.92,
      });
      sharpInput = Buffer.from(jpeg);
    }
    const pipeline = sharp(sharpInput, {
      limitInputPixels: cfg.quotas.max_pixels,
      failOn: 'error',
    })
      .rotate()
      .webp({ quality: 75 });

    const { data, info } = await pipeline.toBuffer({ resolveWithObject: true });
    return { ok: true, webp: data, width: info.width, height: info.height };
  } catch (err) {
    // sharp's pixel-limit error message contains "pixels"; distinguish it from a
    // generic decode failure so callers/tests get a precise reason.
    const msg = err instanceof Error ? err.message.toLowerCase() : '';
    if (msg.includes('pixel')) {
      return { ok: false, reason: 'image too large' };
    }
    return { ok: false, reason: 'corrupt image' };
  }
}
