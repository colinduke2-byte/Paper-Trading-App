import { describe, expect, it } from "vitest";
import { sameQty, sumQty, toUnits } from "./lotmath";

describe("lot quantities", () => {
  it("adds without float error", () => {
    expect(0.1 + 0.2).not.toBe(0.3);
    expect(sumQty([0.1, 0.2])).toBe("0.3");
    expect(sumQty(["0.1", "0.2", "0.0001"])).toBe("0.3001");
    expect(sumQty([1.1, 2.2, 3.3])).toBe("6.6");
    expect(sumQty([5, 5])).toBe("10");
    expect(sumQty([])).toBe("0");
  });
  it("ignores blanks and compares to 4 decimal places", () => {
    expect(sumQty(["", "abc", 2])).toBe("2");
    expect(Number.isNaN(toUnits(""))).toBe(true);
    expect(sameQty("0.3", 0.1 + 0.2)).toBe(true);
    expect(sameQty("0.3", "0.3001")).toBe(false);
  });
});
