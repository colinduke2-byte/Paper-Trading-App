import { describe, expect, it } from "vitest";
import {
  atr, Bar, bollinger, cleanParam, defaultParams, ema, INDICATORS, indicatorLabel, macd, migrateLegacy, parseIndicators, rsi, sma,
  stochastic, trueRange, vwap,
} from "./indicators";

// Reference values: small cases are worked by hand in the comments. The longer series use the 20 closing prices
// of the RSI worked example in StockCharts' ChartSchool (its table rounds the intermediate averages, so it prints
// 70.53 for the first RSI where the unrounded value is 70.464) and an 18-bar OHLC sample. Their expected numbers
// were recomputed with exact rational arithmetic (Python fractions.Fraction) by a separate implementation of the
// textbook definitions, then rounded to 10 decimals.
const TOL = 1e-6;
const close = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThan(TOL);
const nan = (v: number) => expect(Number.isNaN(v)).toBe(true);

const WILDER = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.1, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.0, 46.03, 46.41, 46.22, 45.64];
const H = [48.7, 48.72, 48.9, 48.87, 48.82, 49.05, 49.2, 49.35, 49.92, 50.19, 50.12, 49.66, 49.88, 50.19, 50.36, 50.57, 50.65, 50.43];
const L = [47.79, 48.14, 48.39, 48.37, 48.24, 48.64, 48.94, 48.86, 49.5, 49.87, 49.2, 48.9, 49.43, 49.73, 49.26, 50.09, 50.3, 49.21];
const C = [48.16, 48.61, 48.75, 48.63, 48.74, 49.03, 49.07, 49.32, 49.91, 50.13, 49.53, 49.5, 49.75, 50.03, 50.31, 50.52, 50.41, 49.34];
const OHLC: Bar[] = H.map((h, i) => ({ time: i * 60, open: C[i], high: h, low: L[i], close: C[i], volume: [100, 200, 150, 300][i % 4] }));

describe("sma", () => {
  it("averages the last n values", () => {
    const r = sma([1, 2, 3, 4, 5], 3);
    nan(r[0]); nan(r[1]);
    close(r[2], 2); close(r[3], 3); close(r[4], 4); // (1+2+3)/3, (2+3+4)/3, (3+4+5)/3
  });
  it("skips leading NaNs", () => {
    const r = sma([NaN, NaN, 2, 4, 6], 2);
    nan(r[2]); close(r[3], 3); close(r[4], 5);
  });
  it("is all NaN when there is too little data", () => {
    expect(sma([1, 2], 3).every(Number.isNaN)).toBe(true);
  });
});

describe("ema", () => {
  it("is seeded with the simple average then smoothed with k = 2/(n+1)", () => {
    // n = 3, k = 0.5: seed (1+2+3)/3 = 2 at index 2; then 4*.5+2*.5 = 3; 8*.5+3*.5 = 5.5; 1*.5+5.5*.5 = 3.25.
    const r = ema([1, 2, 3, 4, 8, 1], 3);
    nan(r[1]); close(r[2], 2); close(r[3], 3); close(r[4], 5.5); close(r[5], 3.25);
  });
  it("matches the reference on the Wilder closes", () => {
    const r = ema(WILDER, 10);
    close(r[9], 44.779); // the 10-day simple average: 447.79 / 10
    close(r[10], 44.981); // 45.89 * 2/11 + 44.779 * 9/11
    close(r[19], 45.8703656191);
  });
});

describe("bollinger", () => {
  it("uses the population standard deviation", () => {
    // [1..5]: mean 3, variance (4+1+0+1+4)/5 = 2, so bands at 3 ± 2√2.
    const r = bollinger([1, 2, 3, 4, 5], 5, 2);
    close(r.middle[4], 3); close(r.upper[4], 3 + 2 * Math.SQRT2); close(r.lower[4], 3 - 2 * Math.SQRT2);
    nan(r.upper[3]);
  });
  it("matches the reference on the Wilder closes", () => {
    const r = bollinger(WILDER, 5, 2);
    close(r.middle[19], 46.06); close(r.upper[19], 46.5730302135); close(r.lower[19], 45.5469697865);
  });
});

describe("rsi", () => {
  it("follows Wilder's smoothing on a hand-worked case", () => {
    // n = 2, changes +1 -1 +1 +1. Index 2: avg gain .5, avg loss .5 -> 50.
    // Index 3: gain (.5+1)/2 = .75, loss .25 -> RS 3 -> 75. Index 4: gain .875, loss .125 -> RS 7 -> 87.5.
    const r = rsi([1, 2, 1, 2, 3], 2);
    nan(r[1]); close(r[2], 50); close(r[3], 75); close(r[4], 87.5);
  });
  it("matches Wilder's worked example", () => {
    const r = rsi(WILDER, 14);
    nan(r[13]);
    const want = [70.4641350211, 66.2496185536, 66.4809418347, 69.3468531629, 66.2947126589, 57.9150206701];
    want.forEach((w, j) => close(r[14 + j], w));
  });
  it("is 100 with no losses and 50 with no movement", () => {
    close(rsi([1, 2, 3, 4], 2)[3], 100);
    close(rsi([5, 5, 5, 5], 2)[3], 50);
  });
});

describe("macd", () => {
  it("is fast EMA minus slow EMA with an EMA signal", () => {
    const r = macd(WILDER, 3, 6, 4);
    nan(r.macd[4]);
    close(r.macd[5], 0.2479166667); // first value where the 6-period EMA exists
    nan(r.signal[7]);
    close(r.signal[8], 0.3328404018); // average of the first four MACD values
    close(r.macd[19], -0.0635485212); close(r.signal[19], 0.041704278); close(r.hist[19], -0.1052527992);
  });
  it("histogram is MACD minus signal everywhere it is defined", () => {
    const r = macd(WILDER, 3, 6, 4);
    r.hist.forEach((h, i) => { if (!Number.isNaN(h)) close(h, r.macd[i] - r.signal[i]); });
  });
});

describe("atr", () => {
  it("true range includes gaps from the previous close", () => {
    const tr = trueRange([
      { time: 0, open: 10, high: 11, low: 9, close: 10, volume: 0 },
      { time: 1, open: 13, high: 14, low: 13, close: 13.5, volume: 0 }, // gap up: |14 - 10| = 4
      { time: 2, open: 9, high: 9.5, low: 8, close: 9, volume: 0 }, // gap down: |8 - 13.5| = 5.5
    ]);
    expect(tr).toEqual([2, 4, 5.5]);
  });
  it("matches the reference on the ATR worked example", () => {
    const r = atr(OHLC, 14);
    nan(r[12]);
    close(r[13], 0.5542857143); close(r[14], 0.5932653061); close(r[15], 0.5851749271);
    close(r[16], 0.568376718); close(r[17], 0.6149212382);
  });
});

describe("stochastic", () => {
  it("matches the reference (5, 3, 3)", () => {
    const r = stochastic(OHLC, 5, 3, 3);
    nan(r.k[5]);
    close(r.k[6], 89.9487055737); close(r.k[17], 62.9225262505);
    nan(r.d[7]);
    close(r.d[8], 92.7293708544); close(r.d[17], 82.917883545);
  });
  it("raw %K without smoothing", () => {
    const r = stochastic(OHLC, 5, 1, 3);
    close(r.k[4], 85.5855855856); // (48.74 - 47.79) / (48.90 - 47.79) * 100
    close(r.k[17], 9.0277777778);
  });
  it("is 50 when the range is flat", () => {
    const flat = Array.from({ length: 4 }, (_, i) => ({ time: i, open: 1, high: 1, low: 1, close: 1, volume: 1 }));
    close(stochastic(flat, 2, 1, 1).k[3], 50);
  });
});

describe("vwap", () => {
  it("weights the typical price by volume and restarts each day", () => {
    const bars = OHLC.slice(0, 4);
    const r = vwap(bars, (i) => (i < 2 ? 0 : 1));
    close(r[0], (48.7 + 47.79 + 48.16) / 3); // 48.2166666667
    close(r[1], 48.3988888889);
    close(r[2], 48.68); // a new day: only bar 2
    close(r[3], 48.6422222222);
  });
});

describe("registry", () => {
  it("every indicator computes outputs aligned with the bars", () => {
    for (const [type, def] of Object.entries(INDICATORS)) {
      const outs = def.compute(OHLC, defaultParams(type as any), (i) => Math.floor(i / 10));
      expect(outs.length).toBeGreaterThan(0);
      for (const o of outs) expect(o.values.length).toBe(OHLC.length);
    }
  });
  it("labels show parameters", () => {
    expect(indicatorLabel({ id: "a", type: "macd", params: { fast: 12, slow: 26, signal: 9 } })).toBe("MACD 12 26 9");
    expect(indicatorLabel({ id: "a", type: "vwap", params: {} })).toBe("VWAP");
  });
  it("cleans parameters", () => {
    const p = INDICATORS.sma.params[0];
    expect(cleanParam(p, "30")).toBe(30);
    expect(cleanParam(p, "0")).toBe(1);
    expect(cleanParam(p, "9999")).toBe(500);
    expect(cleanParam(p, "")).toBeNull();
    expect(cleanParam(p, "abc")).toBeNull();
    expect(cleanParam(INDICATORS.bb.params[1], "2.25")).toBe(2.3);
  });
  it("parses stored lists defensively", () => {
    const r = parseIndicators([{ id: "x", type: "sma", params: { period: "50" } }, { type: "nope" }, null, { type: "rsi" }]);
    expect(r.map((c) => [c.type, c.params])).toEqual([["sma", { period: 50 }], ["rsi", { period: 14 }]]);
    expect(r[0].id).toBe("x");
    expect(parseIndicators("junk")).toEqual([]);
  });
  it("migrates the old fixed indicator buttons", () => {
    expect(migrateLegacy("sma20,rsi,bogus").map((c) => [c.type, c.params])).toEqual([["sma", { period: 20 }], ["rsi", { period: 14 }]]);
  });
});
