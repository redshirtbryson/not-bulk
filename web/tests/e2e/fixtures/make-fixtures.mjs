// Generates two E2E photo fixtures: each is a JPEG "photo" containing one
// card-aspect rectangle (0.714 = 2.5/3.5) on a contrasting background with
// margin, so notbulk.detect.detect_cards can find it as an external contour
// (worker/notbulk/detect.py needs a closed quad against contrasting bg — a
// card that fills the whole frame edge-to-edge produces no contour at all).
// The card interior uses a checkerboard (never a flat fill/gradient/noise,
// per the M1 DCT-hashing lesson) so Laplacian sharpness clears
// config.yaml's detection.sharpness_min. Deterministic so the E2E is
// reproducible.
import sharp from 'sharp';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dir = path.dirname(fileURLToPath(import.meta.url));

const PHOTO_W = 1200;
const PHOTO_H = 1600;
const CARD_W = 500;               // 500 / 700 = 0.714 aspect
const CARD_H = 700;
const CARD_X = (PHOTO_W - CARD_W) / 2;
const CARD_Y = (PHOTO_H - CARD_H) / 2;

function checkerboard(label) {
  const cell = 28;
  let squares = '';
  for (let y = 0; y < CARD_H; y += cell) {
    for (let x = 0; x < CARD_W; x += cell) {
      const on = ((x / cell) + (y / cell)) % 2 === 0;
      if (on) squares += `<rect x="${x}" y="${y}" width="${cell}" height="${cell}" fill="#1a1a1a"/>`;
    }
  }
  return `
    <svg xmlns="http://www.w3.org/2000/svg" width="${PHOTO_W}" height="${PHOTO_H}">
      <rect width="${PHOTO_W}" height="${PHOTO_H}" fill="#2d6a2d"/>
      <g transform="translate(${CARD_X},${CARD_Y})">
        <rect x="0" y="0" width="${CARD_W}" height="${CARD_H}" fill="#ffffff"/>
        <g>${squares}</g>
        <rect x="0" y="0" width="${CARD_W}" height="${CARD_H}" fill="none" stroke="#ffffff" stroke-width="30"/>
        <rect x="30" y="30" width="${CARD_W - 60}" height="90" fill="#ffffff"/>
        <text x="55" y="95" font-size="56" font-family="monospace" fill="#111">${label}</text>
      </g>
    </svg>`;
}

async function main() {
  for (const [name, label] of [['card-a.jpg', 'ALPHA'], ['card-b.jpg', 'BETA']]) {
    await sharp(Buffer.from(checkerboard(label))).jpeg({ quality: 92 }).toFile(path.join(dir, name));
  }
  console.log('fixtures written');
}
main();
