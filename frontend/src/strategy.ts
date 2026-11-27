// Strategy templates for the strategy builder: which legs each one has, with default strikes around
// the current price. Pure, like payoff.ts.
import type { Leg } from "./payoff";

export type TemplateId =
  | "bull_call" | "bear_call" | "bear_put" | "bull_put" | "calendar" | "diagonal" | "straddle" | "strangle"
  | "iron_condor" | "butterfly" | "covered_call" | "protective_put" | "custom";

export const TEMPLATES: { id: TemplateId; label: string; hint: string; twoExpiries?: boolean }[] = [
  { id: "bull_call", label: "Bull call spread (call debit vertical)", hint: "Buy a call and sell a higher call: you pay, and profit if the price rises." },
  { id: "bear_call", label: "Bear call spread (call credit vertical)", hint: "Sell a call and buy a higher call: you receive, and profit if the price stays below the short strike." },
  { id: "bear_put", label: "Bear put spread (put debit vertical)", hint: "Buy a put and sell a lower put: you pay, and profit if the price falls." },
  { id: "bull_put", label: "Bull put spread (put credit vertical)", hint: "Sell a put and buy a lower put: you receive, and profit if the price stays above the short strike." },
  { id: "calendar", label: "Calendar spread", hint: "Sell a near option and buy a later one at the same strike.", twoExpiries: true },
  { id: "diagonal", label: "Diagonal spread", hint: "Sell a near option and buy a later one at a different strike.", twoExpiries: true },
  { id: "straddle", label: "Long straddle", hint: "Buy a call and a put at the same strike: profits from a large move either way." },
  { id: "strangle", label: "Long strangle", hint: "Buy a higher call and a lower put: cheaper than a straddle, needs a larger move." },
  { id: "iron_condor", label: "Iron condor", hint: "Sell a put spread and a call spread: you receive, and profit if the price stays between the short strikes." },
  { id: "butterfly", label: "Butterfly", hint: "Buy one low call, sell two middle calls, buy one high call: profits most at the middle strike." },
  { id: "covered_call", label: "Covered call (buy-write)", hint: "Buy 100 shares and sell a call against them." },
  { id: "protective_put", label: "Protective put", hint: "Buy 100 shares and a put that limits the loss below its strike." },
  { id: "custom", label: "Custom (up to 4 legs)", hint: "Add up to four legs: calls, puts and at most one stock leg." },
];

// Index of the strike nearest the current price.
export function atmIndex(strikes: number[], spot: number): number {
  let best = 0;
  strikes.forEach((k, i) => { if (Math.abs(k - spot) < Math.abs(strikes[best] - spot)) best = i; });
  return best;
}

// Legs of a template. `near` is the expiry; `far` the later expiry of calendars and diagonals.
export function templateLegs(id: TemplateId, strikes: number[], spot: number, near: string, far: string): Leg[] {
  if (!strikes.length) return [];
  const at = atmIndex(strikes, spot);
  const k = (steps: number) => strikes[Math.min(Math.max(at + steps, 0), strikes.length - 1)];
  const call = (side: "buy" | "sell", strike: number, ratio = 1, expiry = near): Leg => ({ side, kind: "C", strike, expiry, ratio });
  const put = (side: "buy" | "sell", strike: number, ratio = 1, expiry = near): Leg => ({ side, kind: "P", strike, expiry, ratio });
  const shares: Leg = { side: "buy", kind: "S", strike: 0, expiry: "", ratio: 100 };
  switch (id) {
    case "bull_call": return [call("buy", k(0)), call("sell", k(2))];
    case "bear_call": return [call("sell", k(1)), call("buy", k(3))];
    case "bear_put": return [put("buy", k(0)), put("sell", k(-2))];
    case "bull_put": return [put("sell", k(-1)), put("buy", k(-3))];
    case "calendar": return [call("sell", k(0)), call("buy", k(0), 1, far)];
    case "diagonal": return [call("sell", k(1)), call("buy", k(0), 1, far)];
    case "straddle": return [call("buy", k(0)), put("buy", k(0))];
    case "strangle": return [call("buy", k(2)), put("buy", k(-2))];
    case "iron_condor": return [put("buy", k(-4)), put("sell", k(-2)), call("sell", k(2)), call("buy", k(4))];
    case "butterfly": return [call("buy", k(-2)), call("sell", k(0), 2), call("buy", k(2))];
    case "covered_call": return [shares, call("sell", k(2))];
    case "protective_put": return [shares, put("buy", k(-2))];
    case "custom": return [call("buy", k(0))];
  }
}

// Default expiries: the first one at least three weeks out (enough time value for spreads), and
// for calendars a later one about a month after it.
export function defaultExpiries(exps: { date: string; dte: number }[]): [string, string] {
  if (!exps.length) return ["", ""];
  const near = exps.find((e) => e.dte >= 21) || exps[exps.length - 1];
  const i = exps.indexOf(near);
  const far = exps.find((e) => e.dte >= near.dte + 28) || exps[Math.min(i + 1, exps.length - 1)];
  return [near.date, far.date];
}
