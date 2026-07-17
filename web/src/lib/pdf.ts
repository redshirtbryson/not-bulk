import nunjucks from 'nunjucks';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Config } from '../config.js';

export interface PdfCard {
  cropDataUri: string | null;
  name: string;
  set: string;
  number: string;
  finish: string;
  priceDisplay: string;
  quantity: number;
}
export interface PdfStats {
  totalCards: number;
  totalValueDisplay: string;
  generatedAt: string;
}

/**
 * Minimal structural type for the puppeteer module — the DI seam. The real
 * `puppeteer` default export satisfies this (launch returns a Browser with
 * newPage/close; a Page has setJavaScriptEnabled/setContent/pdf). Unit tests
 * inject a fake; production callers omit the arg and get the real browser.
 */
export interface PuppeteerLike {
  launch(opts?: Record<string, unknown>): Promise<{
    newPage(): Promise<{
      setJavaScriptEnabled(enabled: boolean): Promise<void>;
      setContent(html: string, opts?: Record<string, unknown>): Promise<void>;
      pdf(opts?: Record<string, unknown>): Promise<Buffer | Uint8Array>;
    }>;
    close(): Promise<void>;
  }>;
}

// Standalone Nunjucks env pointed at web/views — mirrors the app's autoescape:true.
// A dedicated env (not the Express one) keeps rendering usable off the request path
// (the export worker renders with no Express app in scope).
const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'views');
const njk = new nunjucks.Environment(new nunjucks.FileSystemLoader(viewsDir), {
  autoescape: true,
  noCache: true,
});

// Module-level concurrency-1 gate: a promise-chain mutex. Each render appends
// itself to the tail; the next render awaits the previous one's settlement
// (success OR failure) before starting. Puppeteer is memory-heavy; one render
// at a time bounds resource use and matches design S8 ("render is concurrency-1").
let renderChain: Promise<unknown> = Promise.resolve();

async function withMutex<T>(fn: () => Promise<T>): Promise<T> {
  const run = renderChain.then(fn, fn); // start after prior settles (ignore prior result/err)
  // Keep the chain alive but never let a rejection poison the next waiter.
  renderChain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

let defaultPuppeteer: PuppeteerLike | null = null;
async function getDefaultPuppeteer(): Promise<PuppeteerLike> {
  if (!defaultPuppeteer) {
    const mod = await import('puppeteer');
    defaultPuppeteer = (mod.default ?? mod) as unknown as PuppeteerLike;
  }
  return defaultPuppeteer;
}

// Test-only seam (Assembly Resolution 5): when set, return a canned minimal PDF
// buffer without launching a browser at all. Checked FIRST, before any Nunjucks
// render or puppeteer import, so it's usable even without views/ or a browser present.
const STUB_PDF = Buffer.from(
  '%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n' +
    '2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF',
  'latin1',
);

/**
 * Render the collection PDF. JS disabled (S8), crops are data URIs so the browser
 * makes zero network requests, browser always closed in finally. Concurrency-1.
 * Throws on any failure (caller marks the export failed).
 */
export async function renderCollectionPdf(
  cards: PdfCard[],
  stats: PdfStats,
  cfg: Config,
  puppeteerImpl?: PuppeteerLike,
): Promise<Buffer> {
  if (process.env.NOTBULK_STUB_PDF) {
    return STUB_PDF;
  }

  const impl = puppeteerImpl ?? (await getDefaultPuppeteer());
  const html = njk.render('collection-pdf.njk', { cards, stats });

  return withMutex(async () => {
    let browser: Awaited<ReturnType<PuppeteerLike['launch']>> | null = null;
    try {
      browser = await launchWithFallback(impl);
      const page = await browser.newPage();
      // S8: JavaScript OFF before any content is loaded.
      await page.setJavaScriptEnabled(false);
      await page.setContent(html, {
        waitUntil: 'load',
        timeout: cfg.export.render_timeout_ms,
      });
      const out = await page.pdf({
        format: cfg.export.page_size,
        printBackground: true,
        timeout: cfg.export.render_timeout_ms,
      });
      return Buffer.isBuffer(out) ? out : Buffer.from(out);
    } finally {
      if (browser) {
        // Never let a close() failure mask the original error / swallow the result.
        await browser.close().catch(() => undefined);
      }
    }
  });
}

/**
 * Launch the bundled chrome-headless-shell for determinism; on an executable-not-found
 * launch failure, fall back to the system Chrome channel (/usr/bin/google-chrome).
 */
async function launchWithFallback(impl: PuppeteerLike) {
  try {
    return await impl.launch({ headless: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (/could not find|executable|ENOENT|browser was not found/i.test(msg)) {
      return await impl.launch({ headless: true, channel: 'chrome' });
    }
    throw err;
  }
}
