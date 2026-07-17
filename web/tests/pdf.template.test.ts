import { describe, it, expect } from 'vitest';
import nunjucks from 'nunjucks';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'views');

function renderPdf(ctx: Record<string, unknown>): string {
  // Standalone env — mirrors app.ts nunjucks.configure autoescape:true, but NOT the app.
  const env = nunjucks.configure(viewsDir, { autoescape: true, noCache: true });
  return env.render('collection-pdf.njk', ctx);
}

const DISCLAIMER =
  'NotBulk is an unofficial fan tool. Not affiliated with, endorsed, or sponsored by ' +
  'Nintendo, Creatures Inc., GAME FREAK Inc., or The Pokémon Company.';

const baseStats = {
  totalCards: 3,
  totalValueDisplay: '$142.50',
  generatedAt: '2026-07-17 14:03 UTC',
};

const goodCard = {
  cropDataUri: 'data:image/webp;base64,UklGRhABBBBB',
  name: 'Charizard',
  set: 'Base Set',
  number: '4/102',
  finish: 'holofoil',
  priceDisplay: '$120.00',
  quantity: 1,
};

const nullCropCard = {
  cropDataUri: null,
  name: 'Pikachu',
  set: 'Jungle',
  number: '60/64',
  finish: 'normal',
  priceDisplay: '$2.50',
  quantity: 2,
};

describe('collection-pdf.njk', () => {
  it('includes the non-affiliation disclaimer VERBATIM (é preserved)', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain(DISCLAIMER);
    expect(html).toContain('Pokémon'); // é, not Pokemon
  });

  it('renders the cover stats: total cards, total value, generated date', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('3');            // totalCards
    expect(html).toContain('$142.50');      // totalValueDisplay
    expect(html).toContain('2026-07-17 14:03 UTC'); // generatedAt
    expect(html).toContain('NotBulk');      // wordmark
    expect(html).toContain('Collection Export');
  });

  it('emits a data-URI <img> for a card with a cropDataUri', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('src="data:image/webp;base64,UklGRhABBBBB"');
  });

  it('emits a placeholder (no <img src=data:>) for a null cropDataUri', () => {
    const html = renderPdf({ cards: [nullCropCard], stats: baseStats });
    // Placeholder box present; NO image element for this card.
    expect(html).toContain('pdf-card__placeholder');
    expect(html).not.toContain('src="data:'); // this single-card render has no data-URI img
  });

  it('renders per-card name/set/number/finish/price/quantity', () => {
    const html = renderPdf({ cards: [goodCard], stats: baseStats });
    expect(html).toContain('Charizard');
    expect(html).toContain('Base Set');
    expect(html).toContain('4/102');
    expect(html).toContain('holofoil');
    expect(html).toContain('$120.00');
  });

  it('ESCAPES a card name containing <script> (no XSS into the PDF)', () => {
    const evil = { ...goodCard, name: '<script>alert(1)</script>' };
    const html = renderPdf({ cards: [evil], stats: baseStats });
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).not.toContain('<script>alert(1)</script>');
  });

  it('contains NO <script> tag anywhere and no external resource references', () => {
    const html = renderPdf({ cards: [goodCard, nullCropCard], stats: baseStats });
    expect(html).not.toMatch(/<script/i);
    expect(html).not.toMatch(/https?:\/\//i); // no external hrefs/srcs
  });
});
