import { describe, expect, it } from "vitest";
import { describeOption, marketableLimit, numText } from "./util";

describe("numText", () => {
  it("removes well-formed thousands separators, dollar signs and spaces", () => {
    expect(numText("1,000")).toBe("1000");
    expect(numText(" $25,000.50 ")).toBe("25000.50");
    expect(numText("1,234,567")).toBe("1234567");
    expect(numText("-$1,000")).toBe("-1000");
    expect(numText("100")).toBe("100");
    expect(numText("")).toBe("");
  });
  it("leaves ambiguous text for the server to refuse with a message", () => {
    expect(numText("1,5")).toBe("1,5");
    expect(numText("1,0000")).toBe("1,0000");
    expect(numText("abc")).toBe("abc");
  });
});

describe("describeOption", () => {
  // the same strings as backend/tests/test_api_fuzz.py::test_option_descriptions_match_the_ui
  it("matches the server's option description", () => {
    expect(describeOption("MKTX261204C00400000")).toBe("MKTX 04 Dec 2026 400 Call");
    expect(describeOption("NVLX270115P00102500")).toBe("NVLX 15 Jan 2027 102.5 Put");
    expect(describeOption("MKTX")).toBe("MKTX");
  });
});

describe("marketableLimit", () => {
  it("goes 2% or at least 5 cents through the quote, to the cent", () => {
    expect(marketableLimit("buy", 1.2).limit).toBe(1.25);
    expect(marketableLimit("sell", 1.2).limit).toBe(1.15);
    expect(marketableLimit("buy", 60).limit).toBe(61.2);
    expect(marketableLimit("sell", 60).limit).toBe(58.8);
    expect(marketableLimit("sell", 0.03).limit).toBe(0.01);
    expect(marketableLimit("buy", 0).limit).toBe(0.05);
  });
  it("says what it did", () => {
    expect(marketableLimit("buy", 1.2).note).toContain("0.05 above the ask (1.20)");
    expect(marketableLimit("sell", 60).note).toContain("1.20 below the bid (60.00)");
  });
});
