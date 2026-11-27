import { expect, it as test } from "vitest";
import {
  analyse, chartRange, curve, describeLeg, Leg, legFromSymbol, midNet, naturalNet, optionSymbol, parseOption, profitAt, recognise, unitsOf,
} from "./payoff";
import { templateLegs, defaultExpiries } from "./strategy";

// Unit tests of the payoff maths in payoff.ts and the templates in strategy.ts (pure modules).
// The figures are the ones backend/tests/test_multileg.py checks for the server's payoff().

const E = "2026-12-18", F = "2027-01-15";
const c = (side: "buy" | "sell", strike: number, ratio = 1, expiry = E): Leg => ({ side, kind: "C", strike, expiry, ratio });
const p = (side: "buy" | "sell", strike: number, ratio = 1, expiry = E): Leg => ({ side, kind: "P", strike, expiry, ratio });
const stock = (ratio = 100): Leg => ({ side: "buy", kind: "S", strike: 0, expiry: "", ratio });

test("payoff of common strategies matches the server's figures", () => {
  const bullCall = [c("buy", 100), c("sell", 105)];
  expect(analyse(bullCall, 2.0)).toMatchObject({ maxLoss: 200, maxProfit: 300, breakEvens: [102], atExpiry: true, maxLossUnlimited: false, maxProfitUnlimited: false });
  expect(analyse([p("sell", 95), p("buy", 90)], -0.9)).toMatchObject({ maxLoss: 410, maxProfit: 90, breakEvens: [94.1] });
  const condor = [p("buy", 90), p("sell", 95), c("sell", 105), c("buy", 110)];
  expect(analyse(condor, -1.7)).toMatchObject({ maxLoss: 330, maxProfit: 170, breakEvens: [93.3, 106.7] });
  expect(analyse([c("buy", 95), c("sell", 100, 2), c("buy", 105)], 1.2)).toMatchObject({ maxLoss: 120, maxProfit: 380, breakEvens: [96.2, 103.8] });
  // Quantity scales profit and loss but not break-evens.
  expect(analyse(bullCall, 2.15, 3)).toMatchObject({ maxLoss: 645, maxProfit: 855, breakEvens: [102.15] });
});

test("unlimited risk, stock legs and mixed expiries", () => {
  const strangle = analyse([c("sell", 105), p("sell", 95)], -3);
  expect(strangle).toMatchObject({ maxLossUnlimited: true, maxLoss: null, maxProfit: 300, breakEvens: [92, 108] });
  const longCall = analyse([c("buy", 100), c("buy", 110)], 5);
  expect(longCall.maxProfitUnlimited).toBe(true);
  expect(longCall.maxProfit).toBeNull();
  // Covered call: 100 shares at 100 less a 2.00 call at 105 is a 98.00 net debit per share.
  expect(analyse([stock(), c("sell", 105)], 98)).toMatchObject({ maxProfit: 700, maxLoss: 9800, breakEvens: [98] });
  expect(analyse([stock(), p("buy", 95)], 102)).toMatchObject({ maxProfitUnlimited: true, maxLoss: 700, breakEvens: [102] });
  // Calendar: loss bounded by the debit, profit not known at a single expiry.
  const cal = analyse([c("sell", 100, 1, E), c("buy", 100, 1, F)], 1.5);
  expect(cal).toMatchObject({ atExpiry: false, maxProfit: null, maxLoss: 150, breakEvens: [] });
  expect(analyse([c("buy", 100, 1, E), c("sell", 100, 1, F)], -1.5).maxLoss).toBeNull();
});

test("profit at a price, the drawn curve and its range", () => {
  const legs = [c("buy", 100), c("sell", 105)];
  expect(profitAt(legs, 2, 2, 90)).toBe(-400);
  expect(profitAt(legs, 2, 2, 102)).toBeCloseTo(0, 6);
  expect(profitAt(legs, 2, 2, 120)).toBe(600);
  const [lo, hi] = chartRange(legs, 101, [102]);
  expect(lo).toBeLessThan(100);
  expect(hi).toBeGreaterThan(105);
  const pts = curve(legs, 2, 1, lo, hi, [102]);
  expect(pts.map((x) => x[0])).toEqual([lo, 100, 102, 105, hi]);
  expect(pts.find((x) => x[0] === 102)![1]).toBeCloseTo(0, 6);
  // The curve is exact: every point agrees with profitAt.
  for (const [x, y] of pts) expect(y).toBeCloseTo(profitAt(legs, 2, 1, x), 9);
});

test("net prices from quotes: buy at the ask, sell at the bid", () => {
  const legs = [c("buy", 100), c("sell", 105)];
  expect(naturalNet(legs, [{ bid: 2.9, ask: 3.1 }, { bid: 1.1, ask: 1.3 }])).toBe(2);
  expect(naturalNet([c("sell", 100), c("buy", 105)], [{ bid: 2.9, ask: 3.1 }, { bid: 1.1, ask: 1.3 }])).toBe(-1.6);
  expect(midNet(legs, [{ bid: 2.9, ask: 3.1 }, { bid: 1.1, ask: 1.3 }])).toBe(1.8);
  expect(naturalNet(legs, [{ bid: 2.9, ask: 3.1 }, null])).toBeNull();
  expect(naturalNet(legs, [{ bid: 2.9, ask: 3.1 }, { bid: 0, ask: 0.05 }])).toBeNull(); // nothing is sold for nothing
  expect(naturalNet([stock(), c("sell", 105)], [{ bid: 99.99, ask: 100 }, { bid: 1.1, ask: 1.3 }])).toBe(98.9);
});

test("option symbols round-trip and legs are described like the server does", () => {
  const s = optionSymbol("MKTX", "2026-12-18", "C", 452.5);
  expect(s).toBe("MKTX261218C00452500");
  expect(parseOption(s)).toEqual({ underlying: "MKTX", expiry: "2026-12-18", kind: "C", strike: 452.5 });
  expect(parseOption("MKTX")).toBeNull();
  expect(describeLeg("MKTX", legFromSymbol(s, "buy"))).toBe("MKTX 18 Dec 2026 452.5 Call");
  expect(legFromSymbol("MKTX", "sell", 100)).toMatchObject({ kind: "S", ratio: 100 });
  expect(unitsOf([2, -4, 2])).toEqual({ units: 2, ratios: [1, 2, 1] });
});

test("strategies are recognised by name", () => {
  expect(recognise([c("buy", 100), c("sell", 105)])).toBe("Bull call spread");
  expect(recognise([c("sell", 100), c("buy", 105)])).toBe("Bear call spread");
  expect(recognise([p("buy", 100), p("sell", 95)])).toBe("Bear put spread");
  expect(recognise([p("sell", 95), p("buy", 90)])).toBe("Bull put spread");
  expect(recognise([c("sell", 100, 1, E), c("buy", 100, 1, F)])).toBe("Calendar call spread");
  expect(recognise([c("sell", 105, 1, E), c("buy", 100, 1, F)])).toBe("Diagonal call spread");
  expect(recognise([c("buy", 100), p("buy", 100)])).toBe("Long straddle");
  expect(recognise([c("sell", 105), p("sell", 95)])).toBe("Short strangle");
  expect(recognise([p("buy", 90), p("sell", 95), c("sell", 105), c("buy", 110)])).toBe("Iron condor");
  expect(recognise([p("buy", 90), p("sell", 100), c("sell", 100), c("buy", 110)])).toBe("Iron butterfly");
  expect(recognise([c("buy", 95), c("sell", 100, 2), c("buy", 105)])).toBe("Long call butterfly");
  expect(recognise([stock(), c("sell", 105)])).toBe("Covered call");
  expect(recognise([stock(), p("buy", 95)])).toBe("Protective put");
  expect(recognise([stock(200), c("sell", 105, 2)])).toBe("Covered call"); // positions: 200 shares, 2 calls
  expect(recognise([stock(150), c("sell", 105)])).toBeNull();
  expect(recognise([c("buy", 100), c("sell", 105, 2)])).toBeNull(); // a ratio spread has no common name here
  expect(recognise([c("buy", 100)])).toBeNull();
});

test("templates put default strikes around the current price", () => {
  const strikes = [430, 440, 450, 460, 470, 480, 490];
  const near = "2026-12-18", far = "2027-01-15";
  expect(templateLegs("bull_call", strikes, 452, near, far)).toEqual([c("buy", 450, 1, near), c("sell", 470, 1, near)]);
  expect(templateLegs("iron_condor", strikes, 458, near, far).map((l) => l.strike)).toEqual([430, 440, 480, 490]); // wings stop at the last listed strike
  expect(templateLegs("calendar", strikes, 452, near, far).map((l) => l.expiry)).toEqual([near, far]);
  expect(templateLegs("covered_call", strikes, 452, near, far)[0]).toMatchObject({ kind: "S", ratio: 100 });
  for (const id of ["bull_call", "bear_call", "bear_put", "bull_put", "straddle", "strangle", "iron_condor", "butterfly", "covered_call", "protective_put"] as const) {
    expect(recognise(templateLegs(id, strikes, 461, near, far)), id).not.toBeNull();
  }
  expect(defaultExpiries([{ date: "a", dte: 4 }, { date: "b", dte: 25 }, { date: "c", dte: 53 }, { date: "d", dte: 88 }])).toEqual(["b", "c"]);
});
