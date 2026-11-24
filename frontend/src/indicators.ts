// Technical indicators as pure functions. Every function returns an array aligned with its input:
// element i is the indicator value at bar i, or NaN where the indicator is not yet defined.
// Chart data is float (see CLAUDE.md: floats are allowed in chart data).

export type Bar = { time: number; open: number; high: number; low: number; close: number; volume: number };

const finite = (v: number) => typeof v === "number" && isFinite(v);
const nans = (n: number) => new Array<number>(n).fill(NaN);
const firstFinite = (xs: number[]) => { const i = xs.findIndex(finite); return i < 0 ? xs.length : i; };

/** Simple moving average over the last n values. Leading NaNs in the input are skipped. */
export function sma(xs: number[], n: number): number[] {
  const out = nans(xs.length);
  const start = firstFinite(xs);
  let sum = 0;
  for (let i = start; i < xs.length; i++) {
    sum += xs[i];
    if (i - start >= n) sum -= xs[i - n];
    if (i - start >= n - 1) out[i] = sum / n;
  }
  return out;
}

/** Exponential moving average, k = 2 / (n + 1), seeded with the simple average of the first n values. */
export function ema(xs: number[], n: number): number[] {
  const out = nans(xs.length);
  const start = firstFinite(xs);
  if (xs.length - start < n) return out;
  const k = 2 / (n + 1);
  let e = 0;
  for (let i = start; i < start + n; i++) e += xs[i];
  e /= n;
  out[start + n - 1] = e;
  for (let i = start + n; i < xs.length; i++) { e = xs[i] * k + e * (1 - k); out[i] = e; }
  return out;
}

/** Bollinger bands: n-period SMA of closes plus and minus mult population standard deviations. */
export function bollinger(closes: number[], n = 20, mult = 2) {
  const middle = sma(closes, n), upper = nans(closes.length), lower = nans(closes.length);
  for (let i = n - 1; i < closes.length; i++) {
    const m = middle[i];
    let v = 0;
    for (let j = i - n + 1; j <= i; j++) v += (closes[j] - m) ** 2;
    const sd = Math.sqrt(v / n);
    upper[i] = m + mult * sd;
    lower[i] = m - mult * sd;
  }
  return { middle, upper, lower };
}

/** Volume-weighted average price of the typical price (H+L+C)/3, restarting at each new day of `dayOf`. */
export function vwap(bars: Bar[], dayOf: (i: number) => number = (i) => Math.floor(bars[i].time / 86400)): number[] {
  const out = nans(bars.length);
  let pv = 0, v = 0, day: number | null = null;
  bars.forEach((b, i) => {
    const d = dayOf(i);
    if (d !== day) { day = d; pv = 0; v = 0; }
    pv += ((b.high + b.low + b.close) / 3) * b.volume;
    v += b.volume;
    if (v > 0) out[i] = pv / v;
  });
  return out;
}

/** Wilder's relative strength index. The first value is at index n. */
export function rsi(closes: number[], n = 14): number[] {
  const out = nans(closes.length);
  let gain = 0, loss = 0;
  for (let i = 1; i < closes.length; i++) {
    const ch = closes[i] - closes[i - 1];
    const g = Math.max(ch, 0), l = Math.max(-ch, 0);
    if (i <= n) { gain += g / n; loss += l / n; } else { gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }
    if (i >= n) out[i] = loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

/** MACD line (fast EMA minus slow EMA), its signal EMA and the histogram (MACD minus signal). */
export function macd(closes: number[], fast = 12, slow = 26, signal = 9) {
  const f = ema(closes, fast), s = ema(closes, slow);
  const line = closes.map((_, i) => (finite(f[i]) && finite(s[i]) ? f[i] - s[i] : NaN));
  const sig = ema(line, signal);
  const hist = line.map((m, i) => (finite(m) && finite(sig[i]) ? m - sig[i] : NaN));
  return { macd: line, signal: sig, hist };
}

/** True range: the largest of high - low, |high - previous close| and |low - previous close|. */
export function trueRange(bars: Bar[]): number[] {
  return bars.map((b, i) => {
    if (i === 0) return b.high - b.low;
    const pc = bars[i - 1].close;
    return Math.max(b.high - b.low, Math.abs(b.high - pc), Math.abs(b.low - pc));
  });
}

/** Wilder's average true range: the first value is the mean of the first n true ranges. */
export function atr(bars: Bar[], n = 14): number[] {
  const tr = trueRange(bars), out = nans(bars.length);
  if (bars.length < n) return out;
  let a = 0;
  for (let i = 0; i < n; i++) a += tr[i];
  a /= n;
  out[n - 1] = a;
  for (let i = n; i < bars.length; i++) { a = (a * (n - 1) + tr[i]) / n; out[i] = a; }
  return out;
}

/** Stochastic oscillator. %K = SMA(smooth) of 100 (C - lowest low) / (highest high - lowest low) over n bars; %D = SMA(d) of %K. */
export function stochastic(bars: Bar[], n = 14, smooth = 3, d = 3) {
  const raw = nans(bars.length);
  for (let i = n - 1; i < bars.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - n + 1; j <= i; j++) { hh = Math.max(hh, bars[j].high); ll = Math.min(ll, bars[j].low); }
    raw[i] = hh === ll ? 50 : (100 * (bars[i].close - ll)) / (hh - ll);
  }
  const k = smooth > 1 ? sma(raw, smooth) : raw;
  return { k, d: sma(k, d) };
}

// ---- indicator registry ---------------------------------------------------------------------------

export type ParamSpec = { key: string; label: string; min: number; max: number; step?: number; def: number };
export type IndicatorType = "sma" | "ema" | "bb" | "vwap" | "volma" | "rsi" | "macd" | "atr" | "stoch";
export type IndicatorConfig = { id: string; type: IndicatorType; params: Record<string, number> };
export type Output = { key: string; label: string; color: string; values: number[]; kind?: "line" | "histogram"; width?: number };
export type IndicatorDef = {
  name: string;
  /** "price": drawn over the price; "volume": over the volume bars; "pane": in its own pane below. */
  place: "price" | "volume" | "pane";
  params: ParamSpec[];
  intradayOnly?: boolean;
  /** Horizontal guide lines in the indicator's pane. */
  guides?: number[];
  compute: (bars: Bar[], p: Record<string, number>, dayOf?: (i: number) => number) => Output[];
};

const period = (def: number, label = "Period"): ParamSpec => ({ key: "period", label, min: 1, max: 500, def });
const closes = (bars: Bar[]) => bars.map((b) => b.close);

export const INDICATORS: Record<IndicatorType, IndicatorDef> = {
  sma: { name: "SMA", place: "price", params: [period(20)],
    compute: (b, p) => [{ key: "v", label: `SMA ${p.period}`, color: "#e0a13a", values: sma(closes(b), p.period) }] },
  ema: { name: "EMA", place: "price", params: [period(21)],
    compute: (b, p) => [{ key: "v", label: `EMA ${p.period}`, color: "#c678dd", values: ema(closes(b), p.period) }] },
  bb: { name: "Bollinger", place: "price",
    params: [period(20), { key: "mult", label: "Deviations", min: 0.1, max: 10, step: 0.1, def: 2 }],
    compute: (b, p) => {
      const r = bollinger(closes(b), p.period, p.mult);
      const tag = `BB ${p.period} ${p.mult}`;
      return [
        { key: "u", label: `${tag} upper`, color: "#7f8ea3", values: r.upper },
        { key: "m", label: `${tag} middle`, color: "#5c6b7f", values: r.middle },
        { key: "l", label: `${tag} lower`, color: "#7f8ea3", values: r.lower },
      ];
    } },
  vwap: { name: "VWAP", place: "price", params: [], intradayOnly: true,
    compute: (b, _p, dayOf) => [{ key: "v", label: "VWAP", color: "#3fc1c9", values: vwap(b, dayOf), width: 2 }] },
  volma: { name: "Volume MA", place: "volume", params: [period(20)],
    compute: (b, p) => [{ key: "v", label: `Vol MA ${p.period}`, color: "#d7b46a", values: sma(b.map((x) => x.volume), p.period) }] },
  rsi: { name: "RSI", place: "pane", params: [period(14)], guides: [70, 30],
    compute: (b, p) => [{ key: "v", label: `RSI ${p.period}`, color: "#e0a13a", values: rsi(closes(b), p.period) }] },
  macd: { name: "MACD", place: "pane",
    params: [{ key: "fast", label: "Fast", min: 1, max: 500, def: 12 }, { key: "slow", label: "Slow", min: 2, max: 500, def: 26 },
      { key: "signal", label: "Signal", min: 1, max: 500, def: 9 }],
    guides: [0],
    compute: (b, p) => {
      const r = macd(closes(b), p.fast, p.slow, p.signal);
      const tag = `MACD ${p.fast} ${p.slow} ${p.signal}`;
      return [
        { key: "h", label: `${tag} histogram`, color: "#5c6b7f", values: r.hist, kind: "histogram" },
        { key: "m", label: tag, color: "#4c9aff", values: r.macd },
        { key: "s", label: `${tag} signal`, color: "#e0a13a", values: r.signal },
      ];
    } },
  atr: { name: "ATR", place: "pane", params: [period(14)],
    compute: (b, p) => [{ key: "v", label: `ATR ${p.period}`, color: "#c678dd", values: atr(b, p.period) }] },
  stoch: { name: "Stochastic", place: "pane", guides: [80, 20],
    params: [period(14), { key: "smooth", label: "%K smoothing", min: 1, max: 100, def: 3 }, { key: "d", label: "%D", min: 1, max: 100, def: 3 }],
    compute: (b, p) => {
      const r = stochastic(b, p.period, p.smooth, p.d);
      const tag = `Stoch ${p.period} ${p.smooth} ${p.d}`;
      return [
        { key: "k", label: `${tag} %K`, color: "#4c9aff", values: r.k },
        { key: "d", label: `${tag} %D`, color: "#e0a13a", values: r.d },
      ];
    } },
};

export function defaultParams(type: IndicatorType): Record<string, number> {
  return Object.fromEntries(INDICATORS[type].params.map((s) => [s.key, s.def]));
}

/** Short name for a configured indicator, such as "SMA 20" or "MACD 12 26 9". */
export function indicatorLabel(c: IndicatorConfig): string {
  const def = INDICATORS[c.type];
  const values = def.params.map((s) => c.params[s.key]);
  return [def.name, ...values].join(" ");
}

/** Clamp a parameter to its allowed range; whole numbers unless the spec has a fractional step. Returns null if not a number. */
export function cleanParam(spec: ParamSpec, v: unknown): number | null {
  const n = typeof v === "string" ? Number(v.trim() === "" ? NaN : v) : (v as number);
  if (typeof n !== "number" || !isFinite(n)) return null;
  const r = spec.step && spec.step < 1 ? Math.round(n / spec.step) * spec.step : Math.round(n);
  return Math.min(spec.max, Math.max(spec.min, Number(r.toFixed(6))));
}

/** Validate a stored indicator list: drops unknown types, fills and clamps parameters. */
export function parseIndicators(raw: unknown): IndicatorConfig[] {
  if (!Array.isArray(raw)) return [];
  const out: IndicatorConfig[] = [];
  for (const r of raw.slice(0, 20)) {
    if (!r || typeof r !== "object" || !((r as any).type in INDICATORS)) continue;
    const type = (r as any).type as IndicatorType;
    const params: Record<string, number> = {};
    for (const s of INDICATORS[type].params) params[s.key] = cleanParam(s, (r as any).params?.[s.key]) ?? s.def;
    const id = typeof (r as any).id === "string" && (r as any).id ? String((r as any).id).slice(0, 40) : newId();
    out.push({ id, type, params });
  }
  return out;
}

/** Indicators chosen with the older fixed buttons ("sma20,rsi") converted to the configurable form. */
export function migrateLegacy(list: string): IndicatorConfig[] {
  const map: Record<string, [IndicatorType, Record<string, number>]> = {
    sma20: ["sma", { period: 20 }], sma50: ["sma", { period: 50 }], ema21: ["ema", { period: 21 }],
    bb: ["bb", { period: 20, mult: 2 }], vwap: ["vwap", {}], rsi: ["rsi", { period: 14 }],
  };
  return list.split(",").filter((k) => k in map).map((k) => ({ id: newId(), type: map[k][0], params: { ...map[k][1] } }));
}

let idSeq = 0;
export function newId(): string {
  return `i${Date.now().toString(36)}${(idSeq++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}
