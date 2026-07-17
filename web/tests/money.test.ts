import { describe, it, expect } from "vitest";
import { formatCents } from "../src/lib/money.js";

describe("formatCents", () => {
  it("formats cents as $X.XX", () => {
    expect(formatCents(1234)).toBe("$12.34");
  });
  it("keeps the trailing zero for whole dollars", () => {
    expect(formatCents(1200)).toBe("$12.00");
  });
  it("keeps the trailing zero for a single-cent-digit remainder", () => {
    expect(formatCents(1230)).toBe("$12.30");
  });
  it("formats zero cents as $0.00", () => {
    expect(formatCents(0)).toBe("$0.00");
  });
});
