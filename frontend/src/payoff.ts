// Pure helpers for multi-leg option orders: option symbols, net prices from quotes, the payoff at expiry
// and the name of a recognisable strategy. No React, no network: unit-tested in e2e/tests/payoff.spec.ts.
//
// A leg is one option (kind "C" or "P") or the stock (kind "S"). For an option leg `ratio` is contracts
// per unit of the order; for the stock leg it is shares per unit. Net prices are per share, the way
// option prices are quoted: one unit costs net x 100. Positive is a net debit (you pay), negative a
// net credit (you receive). This mirrors backend/app/matching/multileg.py.

export type Kind = "C" | "P" | "S";
export type Side = "buy" | "sell";
export type Leg = { side: Side; kind: Kind; strike: number; expiry: string; ratio: number };
export type Px = { bid: number; ask: number } | null | undefined;

const H = 100;
const sgn = (l: { side: Side }) => (l.side === "buy" ? 1 : -1);
export const cents = (x: number) => Math.round((x + (x >= 0 ? 1e-9 : -1e-9)) * 100) / 100;
export const isStock = (l: Leg) => l.kind === "S";
export const legMultiplier = (l: Leg) => (isStock(l) ? 1 : H);

// ---- option symbols (OCC style, as backend/app/options/symbology.py builds them) ----------------
export function optionSymbol(underlying: string, expiry: string, kind: "C" | "P", strike: number): string {
  const [y, m, d] = expiry.split("-");
  return `${underlying}${y.slice(2)}${m}${d}${kind}${String(Math.round(strike * 1000)).padStart(8, "0")}`;
}

export function parseOption(symbol: string): { underlying: string; expiry: string; kind: "C" | "P"; strike: number } | null {
  const m = symbol.match(/^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return null;
  return { underlying: m[1], expiry: `20${m[2]}-${m[3]}-${m[4]}`, kind: m[5] as "C" | "P", strike: Number(m[6]) / 1000 };
}

export const legSymbol = (underlying: string, l: Leg) => (isStock(l) ? underlying : optionSymbol(underlying, l.expiry, l.kind as "C" | "P", l.strike));

export function legFromSymbol(symbol: string, side: Side, ratio = 1): Leg {
  const c = parseOption(symbol);
  return c ? { side, kind: c.kind, strike: c.strike, expiry: c.expiry, ratio } : { side, kind: "S", strike: 0, expiry: "", ratio };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d} ${MONTHS[Number(m) - 1]} ${y}`;
}
export const strikeText = (k: number) => String(Number(k.toFixed(3)));

export function describeLeg(underlying: string, l: Leg): string {
  if (isStock(l)) return `${underlying} shares`;
  return `${underlying} ${shortDate(l.expiry)} ${strikeText(l.strike)} ${l.kind === "C" ? "Call" : "Put"}`;
}

// ---- net price from quotes ---------------------------------------------------------------------
// What the legs cost together right now, buying at the ask and selling at the bid (the price a
// market order would pay). Null when a leg has no usable quote or a leg to be sold has no bid.
export function naturalNet(legs: Leg[], quotes: Px[]): number | null {
  let net = 0;
  for (let i = 0; i < legs.length; i++) {
    const q = quotes[i], l = legs[i];
    if (!q || !(q.ask > 0)) return null;
    const px = l.side === "buy" ? q.ask : q.bid;
    if (!(px > 0)) return null;
    net += sgn(l) * l.ratio * legMultiplier(l) * px;
  }
  return cents(net / H);
}

// The same at the middle of each bid and ask: a fair reference for a limit price.
export function midNet(legs: Leg[], quotes: Px[]): number | null {
  let net = 0;
  for (let i = 0; i < legs.length; i++) {
    const q = quotes[i];
    if (!q || !(q.ask > 0)) return null;
    net += sgn(legs[i]) * legs[i].ratio * legMultiplier(legs[i]) * (q.bid + q.ask) / 2;
  }
  return cents(net / H);
}

// ---- payoff at expiry --------------------------------------------------------------------------
// Value in money of one unit at expiry with the underlying at `price`.
export function unitValue(legs: Leg[], price: number): number {
  let total = 0;
  for (const l of legs) {
    if (isStock(l)) total += sgn(l) * l.ratio * price;
    else {
      const intrinsic = l.kind === "C" ? Math.max(price - l.strike, 0) : Math.max(l.strike - price, 0);
      total += sgn(l) * l.ratio * H * intrinsic;
    }
  }
  return total;
}

// Profit or loss of the whole order (qty units entered at `net` per share) at expiry.
export const profitAt = (legs: Leg[], net: number, qty: number, price: number) => qty * (unitValue(legs, price) - net * H);

export type Analysis = {
  atExpiry: boolean; // false when the legs expire on different dates: profit and break-evens are not known
  maxProfit: number | null; maxProfitUnlimited: boolean;
  maxLoss: number | null; maxLossUnlimited: boolean;
  breakEvens: number[];
};

export function analyse(legs: Leg[], net: number, qty = 1): Analysis {
  const options = legs.filter((l) => !isStock(l));
  const points = [...new Set([0, ...options.map((l) => l.strike)])].sort((a, b) => a - b);
  const values = points.map((p) => unitValue(legs, p) - net * H);
  // Slope of the payoff above the highest strike: net calls plus stock.
  const slope = legs.filter((l) => l.kind !== "P").reduce((s, l) => s + sgn(l) * l.ratio * legMultiplier(l), 0);
  const expiries = new Set(options.map((l) => l.expiry));
  const scale = (v: number) => cents(v * qty);
  const out: Analysis = { atExpiry: expiries.size <= 1, maxProfit: null, maxProfitUnlimited: false, maxLoss: null,
    maxLossUnlimited: slope < 0, breakEvens: [] };
  if (expiries.size > 1) {
    const longs = options.filter((l) => l.side === "buy").map((l) => l.expiry);
    const shorts = options.filter((l) => l.side === "sell").map((l) => l.expiry);
    const maxStr = (xs: string[]) => xs.reduce((a, b) => (a > b ? a : b));
    const minStr = (xs: string[]) => xs.reduce((a, b) => (a < b ? a : b));
    if (slope >= 0 && (!shorts.length || (longs.length && minStr(longs) >= maxStr(shorts)))) out.maxLoss = scale(Math.max(-Math.min(...values), 0));
    return out;
  }
  out.maxProfitUnlimited = slope > 0;
  if (slope <= 0) out.maxProfit = scale(Math.max(Math.max(...values), 0));
  if (slope >= 0) out.maxLoss = scale(Math.max(-Math.min(...values), 0));
  const evens: number[] = [];
  for (let i = 0; i + 1 < points.length; i++) {
    const [a, b, fa, fb] = [points[i], points[i + 1], values[i], values[i + 1]];
    if (Math.abs(fa) < 1e-9) evens.push(a);
    else if ((fa < 0) !== (fb < 0) && Math.abs(fb) >= 1e-9) evens.push(a + ((b - a) * -fa) / (fb - fa));
  }
  const last = points[points.length - 1], fLast = values[values.length - 1];
  if (Math.abs(fLast) < 1e-9) evens.push(last);
  else if (slope !== 0 && (fLast < 0) !== (slope < 0)) evens.push(last + -fLast / slope);
  out.breakEvens = [...new Set(evens.map(cents))].sort((a, b) => a - b);
  return out;
}

// The price range a payoff diagram shows: every strike, break-even and the current price, with room around them.
export function chartRange(legs: Leg[], spot: number, breakEvens: number[]): [number, number] {
  const xs = [spot, ...breakEvens, ...legs.filter((l) => !isStock(l)).map((l) => l.strike)].filter((x) => isFinite(x) && x > 0);
  if (!xs.length) return [0, 1];
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const pad = Math.max((hi - lo) * 0.4, spot * 0.06, 1);
  return [Math.max(0, lo - pad), hi + pad];
}

// Points of the profit and loss line over [lo, hi]. The payoff is straight between strikes, so the
// ends, the strikes and the break-evens are enough to draw it exactly.
export function curve(legs: Leg[], net: number, qty: number, lo: number, hi: number, breakEvens: number[] = []): [number, number][] {
  const xs = [lo, hi, ...legs.filter((l) => !isStock(l)).map((l) => l.strike), ...breakEvens].filter((x) => x >= lo && x <= hi);
  return [...new Set(xs)].sort((a, b) => a - b).map((x) => [x, profitAt(legs, net, qty, x)]);
}

// ---- strategy names ----------------------------------------------------------------------------
const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : Math.abs(a));

// Splits signed position sizes into a number of units and a whole ratio per leg.
export function unitsOf(sizes: number[]): { units: number; ratios: number[] } {
  const abs = sizes.map((s) => Math.round(Math.abs(s)));
  const units = abs.reduce((g, s) => gcd(g, s), 0) || 1;
  return { units, ratios: abs.map((s) => s / units) };
}

// The usual name of a combination of legs, or null when it is not one of the common strategies.
export function recognise(input: Leg[]): string | null {
  if (input.length < 2 || input.length > 4) return null;
  const { ratios } = unitsOf(input.map((l) => l.ratio * (isStock(l) ? 1 : 100)));
  const legs = input.map((l, i) => ({ ...l, r: ratios[i] }));
  const stock = legs.filter(isStock), opts = legs.filter((l) => !isStock(l));
  const sameExpiry = new Set(opts.map((l) => l.expiry)).size === 1;
  const by = (k: Kind) => opts.filter((l) => l.kind === k).sort((a, b) => a.strike - b.strike);
  const calls = by("C"), puts = by("P");
  const kindName = (k: Kind) => (k === "C" ? "call" : "put");
  if (stock.length > 1) return null;
  if (stock.length === 1) {
    const s = stock[0];
    if (s.side !== "buy") return null;
    const covers = (c: typeof legs[number]) => c.r === s.r; // ratios are in shares here: one contract = 100
    if (opts.length === 1 && calls.length === 1 && calls[0].side === "sell" && covers(calls[0])) return "Covered call";
    if (opts.length === 1 && puts.length === 1 && puts[0].side === "buy" && covers(puts[0])) return "Protective put";
    if (opts.length === 2 && calls.length === 1 && puts.length === 1 && calls[0].side === "sell" && puts[0].side === "buy"
      && covers(calls[0]) && covers(puts[0]) && puts[0].strike <= calls[0].strike) return "Collar";
    return null;
  }
  if (opts.length === 2) {
    const [a, b] = opts;
    const equal = a.r === b.r;
    if (a.kind === b.kind && a.side !== b.side && equal) {
      if (sameExpiry) {
        const [lo, hi] = a.strike < b.strike ? [a, b] : [b, a];
        if (lo.strike === hi.strike) return null;
        if (a.kind === "C") return lo.side === "buy" ? "Bull call spread" : "Bear call spread";
        return hi.side === "buy" ? "Bear put spread" : "Bull put spread";
      }
      return `${a.strike === b.strike ? "Calendar" : "Diagonal"} ${kindName(a.kind)} spread`;
    }
    if (calls.length === 1 && puts.length === 1 && a.side === b.side && equal && sameExpiry) {
      const long = a.side === "buy" ? "Long" : "Short";
      return calls[0].strike === puts[0].strike ? `${long} straddle` : puts[0].strike < calls[0].strike ? `${long} strangle` : null;
    }
    return null;
  }
  if (opts.length === 3 && sameExpiry && (calls.length === 3 || puts.length === 3)) {
    const [l, m, h] = calls.length === 3 ? calls : puts;
    if (l.r === 1 && m.r === 2 && h.r === 1 && l.side === h.side && m.side !== l.side && m.strike - l.strike === h.strike - m.strike && l.strike < m.strike)
      return `${l.side === "buy" ? "Long" : "Short"} ${kindName(l.kind)} butterfly`;
    return null;
  }
  if (opts.length === 4 && sameExpiry && calls.length === 2 && puts.length === 2 && opts.every((l) => l.r === opts[0].r)) {
    const [pl, ph] = puts, [cl, ch] = calls;
    const short = pl.side === "buy" && ph.side === "sell" && cl.side === "sell" && ch.side === "buy";
    const long = pl.side === "sell" && ph.side === "buy" && cl.side === "buy" && ch.side === "sell";
    if ((short || long) && ph.strike <= cl.strike && pl.strike < ph.strike && cl.strike < ch.strike)
      return `${long ? "Reverse " : ""}iron ${ph.strike === cl.strike ? "butterfly" : "condor"}`.replace(/^./, (c) => c.toUpperCase());
    return null;
  }
  return null;
}
