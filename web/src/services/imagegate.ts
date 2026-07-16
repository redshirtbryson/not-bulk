// Placeholder image-gating function. A later task provides the real
// dimension/byte-size/format gate; app.ts only needs the type for its
// optional DI seam today.
export interface ImageGateResult {
  ok: boolean;
  reason?: string;
}

export function gateImage(_buffer: Buffer): ImageGateResult {
  throw new Error("gateImage is not implemented yet; a later task wires the real image gate");
}
