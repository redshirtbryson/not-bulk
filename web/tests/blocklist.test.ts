import { describe, it, expect } from "vitest";
import { isDisposable } from "../src/services/blocklist.js";

describe("isDisposable", () => {
  it("flags a listed domain", () => {
    expect(isDisposable("someone@mailinator.com")).toBe(true);
  });

  it("passes a normal domain", () => {
    expect(isDisposable("bryson@cutlerwv.com")).toBe(false);
    expect(isDisposable("user@gmail.com")).toBe(false);
  });

  it("flags a subdomain of a listed domain", () => {
    expect(isDisposable("bot@a.mailinator.com")).toBe(true);
    expect(isDisposable("bot@deep.sub.mailinator.com")).toBe(true);
  });

  it("returns false for malformed input", () => {
    expect(isDisposable("no-at-sign")).toBe(false);
    expect(isDisposable("trailing@")).toBe(false);
  });
});
