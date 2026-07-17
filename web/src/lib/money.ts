// Single source of truth for cents -> dollar-string formatting. Non-null only: callers decide
// how to render a null price (view: "no price data" / "pending price"; CSV: "").
export function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}
