import { describe, it, expect } from 'vitest';
import { renderCollectionPdf, type PdfCard, type PdfStats } from '../src/lib/pdf.js';
import type { Config } from '../src/config.js';

// Gated: only runs with PDF_RENDER=1 (mirrors the STORAGE_INTEGRATION pattern).
const gated = process.env.PDF_RENDER === '1' ? describe : describe.skip;

const cfg = {
  export: { page_size: 'Letter', render_timeout_ms: 30000 },
} as unknown as Config;

const cards: PdfCard[] = [
  { cropDataUri: null, name: 'Charizard', set: 'Base Set', number: '4/102', finish: 'holofoil', priceDisplay: '$120.00', quantity: 1 },
  { cropDataUri: null, name: 'Pikachu', set: 'Jungle', number: '60/64', finish: 'normal', priceDisplay: '$2.50', quantity: 2 },
];
const stats: PdfStats = { totalCards: 2, totalValueDisplay: '$122.50', generatedAt: '2026-07-17 14:03 UTC' };

gated('renderCollectionPdf (real browser, PDF_RENDER=1)', () => {
  it('produces a real, non-trivial PDF buffer', async () => {
    const buf = await renderCollectionPdf(cards, stats, cfg);
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString('latin1')).toBe('%PDF'); // PDF magic
    expect(buf.length).toBeGreaterThan(1000);                    // non-trivial
  }, 60_000);
});
