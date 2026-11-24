// The imperative side of the chart: one "generation" per lightweight-charts instance. A generation is rebuilt
// whenever the symbol, timeframe, chart type, indicator set or compare symbol changes. Every function here checks
// `g.alive` first, so nothing ever touches a removed chart ("Object is disposed").
import {
  AreaSeries, BarSeries, CandlestickSeries, ColorType, createChart, HistogramSeries, IChartApi, ISeriesApi, LineSeries, Logical,
  MouseEventParams, PriceScaleMode, SeriesType, Time,
} from "lightweight-charts";
import { Bar, IndicatorConfig, INDICATORS, Output } from "../indicators";
import { Anchor, logicalToTime, timeToLogical, XY } from "./drawings";
import { DrawingScene, DrawingsPrimitive } from "./primitive";
import type { ChartType } from "./state";

export const TF_SECONDS: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400, "1w": 604800 };
export const INTRADAY = new Set(["1m", "5m", "15m", "1h"]);
export const COLORS = { up: "#2fbf71", down: "#ef5350", grid: "#1d232c", text: "#8591a1", bg: "#12161c" };

type Ind = { cfg: IndicatorConfig; outputs: Output[]; series: ISeriesApi<SeriesType>[]; pane: number };

export type Gen = {
  alive: boolean;
  key: string;
  symbol: string;
  tf: string;
  type: ChartType;
  intraday: boolean;
  shift: number;
  step: number;
  chart: IChartApi;
  main: ISeriesApi<SeriesType>;
  volume: ISeriesApi<"Histogram">;
  compare: ISeriesApi<"Line"> | null;
  compareSymbol: string;
  primitive: DrawingsPrimitive;
  bars: Bar[]; // times shifted to local time for intraday display
  raw: number[]; // the real UTC times of the bars
  inds: Ind[];
  loaded: boolean;
  viewport: Set<() => void>;
  hover: Set<(i: number | null) => void>;
  hovered: number | null;
  emitViewport: () => void;
};

export function create(el: HTMLElement, opts: {
  symbol: string; tf: string; type: ChartType; indicators: IndicatorConfig[]; compare: string; log: boolean;
  scene: (g: Gen) => DrawingScene;
}): Gen {
  const intraday = INTRADAY.has(opts.tf);
  const chart = createChart(el, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: COLORS.bg }, textColor: COLORS.text, fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    timeScale: { timeVisible: intraday, secondsVisible: false, borderColor: COLORS.grid },
    rightPriceScale: { borderColor: COLORS.grid },
    crosshair: { mode: 0 },
  });
  const common = { priceLineVisible: true, lastValueVisible: true };
  let main: ISeriesApi<SeriesType>;
  if (opts.type === "bars") main = chart.addSeries(BarSeries, { ...common, upColor: COLORS.up, downColor: COLORS.down, thinBars: false });
  else if (opts.type === "line") main = chart.addSeries(LineSeries, { ...common, color: "#4c9aff", lineWidth: 2 });
  else if (opts.type === "area") main = chart.addSeries(AreaSeries, { ...common, lineColor: "#4c9aff", topColor: "#4c9aff55", bottomColor: "#4c9aff05", lineWidth: 2 });
  else main = chart.addSeries(CandlestickSeries, { ...common, upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down });
  const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  // Only the main pane's scale: indicator panes keep plain values.
  main.priceScale().applyOptions({ mode: scaleMode(opts.log, !!opts.compare) });

  const line = (color: string, pane: number, width = 1, scale?: string) =>
    chart.addSeries(LineSeries, { color, lineWidth: width as any, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      ...(scale != null ? { priceScaleId: scale } : {}) }, pane);
  const inds: Ind[] = [];
  let pane = 0;
  for (const cfg of opts.indicators) {
    const def = INDICATORS[cfg.type];
    if (def.intradayOnly && !intraday) continue;
    const p = def.place === "pane" ? ++pane : 0;
    // Compute once on no data just to learn the outputs' shape.
    const shape = def.compute([], cfg.params);
    const series = shape.map((o) => o.kind === "histogram"
      ? chart.addSeries(HistogramSeries, { color: o.color, priceLineVisible: false, lastValueVisible: false }, p) as ISeriesApi<SeriesType>
      : line(o.color, p, o.width ?? 1, def.place === "volume" ? "" : undefined) as ISeriesApi<SeriesType>);
    for (const v of def.guides || [])
      series[series.length - 1].createPriceLine({ price: v, color: "#5c6b7f", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    inds.push({ cfg, outputs: [], series, pane: p });
  }
  const compare = opts.compare ? chart.addSeries(LineSeries, { color: "#e5c07b", lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: opts.compare }) : null;

  let frame = 0;
  const g: Gen = {
    alive: true, key: `${opts.symbol}|${opts.tf}`, symbol: opts.symbol, tf: opts.tf, type: opts.type, intraday,
    shift: intraday ? -new Date().getTimezoneOffset() * 60 : 0, step: TF_SECONDS[opts.tf] || 60,
    chart, main, volume, compare, compareSymbol: opts.compare, primitive: null as any, bars: [], raw: [], inds, loaded: false,
    viewport: new Set(), hover: new Set(), hovered: null,
    emitViewport: () => {
      if (frame || !g.alive) return;
      frame = requestAnimationFrame(() => { frame = 0; if (g.alive) for (const f of g.viewport) f(); });
    },
  };
  g.primitive = new DrawingsPrimitive(() => opts.scene(g), g.emitViewport);
  main.attachPrimitive(g.primitive);
  chart.subscribeCrosshairMove((p: MouseEventParams<Time>) => {
    if (!g.alive) return;
    const i = p.logical != null && p.point ? Math.round(p.logical) : null;
    g.hovered = i != null && i >= 0 && i < g.bars.length ? i : null;
    for (const f of g.hover) f(g.hovered);
  });
  return g;
}

export function scaleMode(log: boolean, comparing: boolean) {
  return comparing ? PriceScaleMode.Percentage : log ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal;
}

export function destroy(g: Gen) {
  if (!g.alive) return;
  g.alive = false;
  g.viewport.clear(); g.hover.clear();
  g.chart.remove();
}

const mainPoint = (g: Gen, b: Bar) => (g.type === "line" || g.type === "area" ? { time: b.time as Time, value: b.close } : { ...b, time: b.time as Time });
const volPoint = (b: Bar) => ({ time: b.time as Time, value: b.volume, color: b.close >= b.open ? "#2fbf7155" : "#ef535055" });

function computeIndicators(g: Gen) {
  const dayOf = (i: number) => Math.floor(g.raw[i] / 86400);
  for (const ind of g.inds) ind.outputs = INDICATORS[ind.cfg.type].compute(g.bars, ind.cfg.params, dayOf);
}

function histColor(v: number) { return v >= 0 ? "#2fbf7188" : "#ef535088"; }

/** Replace all data with freshly loaded bars (real UTC times). */
export function setBars(g: Gen, raw: Bar[]) {
  if (!g.alive) return;
  g.raw = raw.map((b) => b.time);
  g.bars = raw.map((b) => ({ ...b, time: b.time + g.shift }));
  g.main.setData(g.bars.map((b) => mainPoint(g, b)) as any);
  g.volume.setData(g.bars.map(volPoint) as any);
  computeIndicators(g);
  for (const ind of g.inds)
    ind.outputs.forEach((o, k) => {
      const pts: any[] = [];
      o.values.forEach((v, i) => { if (isFinite(v)) pts.push(o.kind === "histogram" ? { time: g.bars[i].time, value: v, color: histColor(v) } : { time: g.bars[i].time, value: v }); });
      ind.series[k].setData(pts);
    });
  g.loaded = true;
}

export function setCompare(g: Gen, raw: Bar[]) {
  if (!g.alive || !g.compare) return;
  const shifted = new Set(g.bars.map((b) => b.time));
  // Only times the main symbol also has, so the time axis (and every drawing's position) is unchanged.
  g.compare.setData(raw.map((b) => ({ time: (b.time + g.shift) as Time, value: b.close })).filter((p) => shifted.has(p.time as number)));
}

/** Stream a trade price into the last bar (or start a new one) and update every indicator's latest value. */
export function streamPrice(g: Gen, price: number, ts: number) {
  if (!g.alive || !g.loaded || !g.bars.length || !isFinite(price)) return;
  const last = g.bars[g.bars.length - 1];
  if (g.intraday) {
    const rawT = Math.floor(ts / g.step) * g.step;
    const t = rawT + g.shift;
    if (t > last.time) {
      g.bars.push({ time: t, open: price, high: price, low: price, close: price, volume: 0 });
      g.raw.push(rawT);
    } else if (t === last.time) Object.assign(last, { high: Math.max(last.high, price), low: Math.min(last.low, price), close: price });
    else return;
  } else {
    Object.assign(last, { high: Math.max(last.high, price), low: Math.min(last.low, price), close: price });
  }
  const b = g.bars[g.bars.length - 1];
  try {
    g.main.update(mainPoint(g, b) as any);
    if (b !== last) g.volume.update(volPoint(b) as any);
    computeIndicators(g);
    const i = g.bars.length - 1;
    for (const ind of g.inds)
      ind.outputs.forEach((o, k) => {
        const v = o.values[i];
        if (isFinite(v)) ind.series[k].update((o.kind === "histogram" ? { time: b.time, value: v, color: histColor(v) } : { time: b.time, value: v }) as any);
      });
  } catch { /* an update older than the last bar is ignored */ }
}

// ---- coordinates ---------------------------------------------------------------------------------------

/** The main pane's plot area in pixels relative to the chart element. */
export function plotRect(g: Gen) {
  if (!g.alive) return { left: 0, top: 0, width: 0, height: 0 };
  let left = 0;
  try { left = g.chart.priceScale("left").width(); } catch {}
  return { left, top: 0, width: g.chart.timeScale().width(), height: g.chart.panes()[0]?.getHeight() ?? 0 };
}

function axis(g: Gen) {
  const ts = g.chart.timeScale();
  const c0 = ts.logicalToCoordinate(0 as Logical), c1 = ts.logicalToCoordinate(1 as Logical);
  if (c0 == null || c1 == null || c1 === c0) return null;
  return { c0: c0 as number, span: (c1 as number) - (c0 as number) };
}

/** Anchor (UTC time, price) to pixels inside the main pane. */
export function toPx(g: Gen, a: Anchor): XY | null {
  if (!g.alive || !g.raw.length) return null;
  const ax = axis(g);
  const l = timeToLogical(g.raw, a.t, g.step);
  const y = g.main.priceToCoordinate(a.p);
  if (!ax || l == null || y == null) return null;
  return { x: ax.c0 + l * ax.span, y };
}

/** Pixels inside the main pane to an anchor. */
export function fromPx(g: Gen, p: XY): Anchor | null {
  if (!g.alive || !g.raw.length) return null;
  const ax = axis(g);
  const price = g.main.coordinateToPrice(p.y);
  if (!ax || price == null || !(price > 0)) return null;
  const t = logicalToTime(g.raw, (p.x - ax.c0) / ax.span, g.step);
  return t == null ? null : { t: Math.round(t), p: price as number };
}

export function priceAt(g: Gen, y: number): number | null {
  if (!g.alive) return null;
  const p = g.main.coordinateToPrice(y);
  return p == null || !(p > 0) ? null : Math.round((p as number) * 100) / 100;
}

export function defaultView(g: Gen) {
  if (!g.alive) return;
  const n = g.bars.length;
  g.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 4 });
  g.main.priceScale().setAutoScale(true);
  sizePanes(g);
}

/** Give the price pane most of the height and share the rest between indicator panes. */
export function sizePanes(g: Gen) {
  if (!g.alive) return;
  const panes = g.chart.panes();
  if (panes.length < 2) return;
  panes[0].setStretchFactor(Math.max(3, (panes.length - 1) * 1.5));
  for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1);
}
