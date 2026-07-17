import { describe, it, expect, afterEach } from 'vitest';
import { renderCollectionPdf, type PuppeteerLike, type PdfCard, type PdfStats } from '../src/lib/pdf.js';
import type { Config } from '../src/config.js';

const cfg = {
  export: { page_size: 'Letter', render_timeout_ms: 30000 },
} as unknown as Config;

const cards: PdfCard[] = [
  { cropDataUri: 'data:image/webp;base64,UklGRhAA', name: 'Charizard', set: 'Base Set', number: '4/102', finish: 'holofoil', priceDisplay: '$120.00', quantity: 1 },
];
const stats: PdfStats = { totalCards: 1, totalValueDisplay: '$120.00', generatedAt: '2026-07-17 14:03 UTC' };

// A fake puppeteer recording calls. `onPdf` lets a test control the pdf() step
// (reject, or gate on a deferred to prove serialization).
function makeFake(opts: { onPdf?: () => Promise<Buffer> } = {}) {
  const calls: string[] = [];
  const setContentHtml: string[] = [];
  const pdfOptions: any[] = [];
  let jsEnabledArg: boolean | null = null;
  let closes = 0;
  let launches = 0;
  const page = {
    async setJavaScriptEnabled(v: boolean) { jsEnabledArg = v; calls.push('setJavaScriptEnabled:' + v); },
    async setContent(html: string, _o: any) { setContentHtml.push(html); calls.push('setContent'); },
    async pdf(o: any) {
      pdfOptions.push(o);
      calls.push('pdf');
      if (opts.onPdf) return opts.onPdf();
      return Buffer.from('%PDF-1.4 fake');
    },
  };
  const browser = {
    async newPage() { calls.push('newPage'); return page; },
    async close() { closes++; calls.push('close'); },
  };
  const impl: PuppeteerLike = {
    async launch(_o?: any) { launches++; calls.push('launch'); return browser as any; },
  };
  return { impl, calls, setContentHtml, pdfOptions, get jsEnabledArg() { return jsEnabledArg; }, get closes() { return closes; }, get launches() { return launches; } };
}

const DISCLAIMER =
  'NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by ' +
  'Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.';

describe('renderCollectionPdf (unit, DI fake)', () => {
  it('disables JavaScript (S8) before setting content', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.jsEnabledArg).toBe(false);
    // Order: setJavaScriptEnabled must come before setContent.
    expect(f.calls.indexOf('setJavaScriptEnabled:false')).toBeLessThan(f.calls.indexOf('setContent'));
  });

  it('renders the disclaimer HTML into setContent', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.setContentHtml[0]).toContain(DISCLAIMER);
    expect(f.setContentHtml[0]).toContain('Charizard');
  });

  it('calls page.pdf with the configured format and printBackground', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.pdfOptions[0]).toMatchObject({ format: 'Letter', printBackground: true });
  });

  it('returns the pdf Buffer', async () => {
    const f = makeFake();
    const buf = await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString('latin1')).toBe('%PDF');
  });

  it('closes the browser in finally even when pdf() rejects, and rethrows', async () => {
    const f = makeFake({ onPdf: async () => { throw new Error('boom'); } });
    await expect(renderCollectionPdf(cards, stats, cfg, f.impl)).rejects.toThrow('boom');
    expect(f.closes).toBe(1); // finally ran
  });

  it('serializes concurrent renders (concurrency-1 mutex): no overlap', async () => {
    let active = 0;
    let maxActive = 0;
    const gate: Array<() => void> = [];
    const onPdf = () =>
      new Promise<Buffer>((resolve) => {
        active++;
        maxActive = Math.max(maxActive, active);
        // Hold the render open until released, so if the mutex were broken the
        // second render would enter here concurrently and push active to 2.
        gate.push(() => { active--; resolve(Buffer.from('%PDF-1.4')); });
      });
    const f1 = makeFake({ onPdf });
    const f2 = makeFake({ onPdf });

    const p1 = renderCollectionPdf(cards, stats, cfg, f1.impl);
    const p2 = renderCollectionPdf(cards, stats, cfg, f2.impl);

    // Release both once the first has entered; a working mutex means only one
    // render is ever active, so gate never has 2 pending at once.
    const release = setInterval(() => { if (gate.length) gate.shift()!(); }, 5);
    await Promise.all([p1, p2]);
    clearInterval(release);

    expect(maxActive).toBe(1); // never two renders in flight
    expect(f1.launches).toBe(1);
    expect(f2.launches).toBe(1);
  });
});

describe('renderCollectionPdf — NOTBULK_STUB_PDF seam (Assembly Resolution 5)', () => {
  afterEach(() => {
    delete process.env.NOTBULK_STUB_PDF;
  });

  it('returns a canned %PDF buffer WITHOUT touching puppeteer when the env var is set', async () => {
    process.env.NOTBULK_STUB_PDF = '1';
    const f = makeFake();
    const buf = await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(Buffer.isBuffer(buf)).toBe(true);
    expect(buf.subarray(0, 4).toString('latin1')).toBe('%PDF');
    // The DI fake was never invoked: no launch, no newPage, no pdf() call.
    expect(f.launches).toBe(0);
    expect(f.calls).toEqual([]);
  });

  it('renders normally (fake puppeteer invoked) when the env var is unset', async () => {
    const f = makeFake();
    await renderCollectionPdf(cards, stats, cfg, f.impl);
    expect(f.launches).toBe(1);
  });
});
