import { describe, expect, it } from "vitest";
import { safeUrl } from "./safeUrl";

describe("safeUrl", () => {
  it("keeps absolute http and https links", () => {
    expect(safeUrl("https://example.com/a?b=1")).toBe("https://example.com/a?b=1");
    expect(safeUrl("http://example.com")).toBe("http://example.com/");
  });
  it("refuses every other scheme, relative paths and junk", () => {
    for (const bad of ["javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "vbscript:x", "/api/x",
                       "//evil.example", "", "not a url", null, undefined, 42, {}, "https://" + "a".repeat(3000)]) {
      expect(safeUrl(bad)).toBeUndefined();
    }
  });
});
