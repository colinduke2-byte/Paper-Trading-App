import { useEffect, useRef, useState } from "react";
import { CandlestickSeries, ColorType, HistogramSeries, LineSeries, createChart } from "lightweight-charts";
import { api, store } from "./util";

const TFS = ["1m", "5m", "15m", "1h", "1d", "1w"];
const TF_SECONDS: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };
const INDICATORS: Record<string, string> = {
  sma20: "SMA 20", sma50: "SMA 50", ema21: "EMA 21", bb: "Bollinger", vwap: "VWAP", rsi: "RSI 14",
};
const COLORS = { up: "#2fbf71", down: "#ef5350", grid: "#1d232c", text: "#8591a1", bg: "#12161c" };

type Bar = { time: number; open: number; high: number; low: number; close: number; volume: number };

function sma(bars: Bar[], n: number) {
  const out: any[] = [];
  let sum = 0;
  bars.forEach((b, i) => {
    sum += b.close;
    if (i >= n) sum -= bars[i - n].close;
    if (i >= n - 1) out.push({ time: b.time, value: sum / n });
  });
  return out;
}

function ema(bars: Bar[], n: number) {
  const out: any[] = [];
  const k = 2 / (n + 1);
  let e = 0;
  bars.forEach((b, i) => {
    e = i === 0 ? b.close : b.close * k + e * (1 - k);
    if (i >= n - 1) out.push({ time: b.time, value: e });
  });
  return out;
}

function bollinger(bars: Bar[], n = 20, mult = 2) {
  const upper: any[] = [], lower: any[] = [];
  for (let i = n - 1; i < bars.length; i++) {
    const w = bars.slice(i - n + 1, i + 1).map((b) => b.close);
    const mean = w.reduce((a, b) => a + b, 0) / n;
    const sd = Math.sqrt(w.reduce((a, b) => a + (b - mean) ** 2, 0) / n);
    upper.push({ time: bars[i].time, value: mean + mult * sd });
    lower.push({ time: bars[i].time, value: mean - mult * sd });
  }
  return { upper, lower };
}

function vwap(bars: Bar[], rawTimes: number[]) {
  const out: any[] = [];
  let pv = 0, v = 0, day = -1;
  bars.forEach((b, i) => {
    const d = Math.floor(rawTimes[i] / 86400);
    if (d !== day) { day = d; pv = 0; v = 0; }
    pv += ((b.high + b.low + b.close) / 3) * b.volume;
    v += b.volume;
    if (v > 0) out.push({ time: b.time, value: pv / v });
  });
  return out;
}

function rsi(bars: Bar[], n = 14) {
  const out: any[] = [];
  let gain = 0, loss = 0;
  for (let i = 1; i < bars.length; i++) {
    const ch = bars[i].close - bars[i - 1].close;
    const g = Math.max(ch, 0), l = Math.max(-ch, 0);
    if (i <= n) { gain += g / n; loss += l / n; }
    else { gain = (gain * (n - 1) + g) / n; loss = (loss * (n - 1) + l) / n; }
    if (i >= n) out.push({ time: bars[i].time, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) });
  }
  return out;
}

export function Chart({ symbol, quote, position, orders }: { symbol: string; quote: any; position: any; orders: any[] }) {
  const el = useRef<HTMLDivElement>(null);
  const [tf, setTf] = useState(() => store("pd.tf") || "5m");
  const [inds, setInds] = useState<string[]>(() => (store("pd.inds") || "sma20").split(",").filter(Boolean));
  const [error, setError] = useState("");
  const ref = useRef<any>({});
  const intraday = tf in TF_SECONDS;
  // Intraday bars are shown in the viewer's local time.
  const shift = intraday ? -new Date().getTimezoneOffset() * 60 : 0;

  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: COLORS.bg }, textColor: COLORS.text, fontSize: 11 },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      timeScale: { timeVisible: intraday, secondsVisible: false, borderColor: COLORS.grid },
      rightPriceScale: { borderColor: COLORS.grid },
      crosshair: { mode: 0 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    });
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const line = (color: string, pane = 0, width = 1) =>
      chart.addSeries(LineSeries, { color, lineWidth: width as any, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, pane);
    const series: Record<string, any> = {};
    if (inds.includes("sma20")) series.sma20 = line("#e0a13a");
    if (inds.includes("sma50")) series.sma50 = line("#4c9aff");
    if (inds.includes("ema21")) series.ema21 = line("#c678dd");
    if (inds.includes("bb")) { series.bbU = line("#5c6b7f"); series.bbL = line("#5c6b7f"); }
    if (inds.includes("vwap") && intraday) series.vwap = line("#3fc1c9", 0, 2);
    if (inds.includes("rsi")) {
      series.rsi = line("#e0a13a", 1);
      series.rsi.createPriceLine({ price: 70, color: "#5c6b7f", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
      series.rsi.createPriceLine({ price: 30, color: "#5c6b7f", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    }
    ref.current = { chart, candles, volume, last: null, lines: [] };
    let alive = true, first = true;

    const load = async () => {
      try {
        const raw: Bar[] = await api(`/bars/${symbol}?tf=${tf}&limit=${intraday ? 1500 : 520}`);
        if (!alive) return;
        const rawTimes = raw.map((b) => b.time);
        const bars = raw.map((b) => ({ ...b, time: b.time + shift }));
        candles.setData(bars as any);
        volume.setData(bars.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "#2fbf7155" : "#ef535055" })) as any);
        series.sma20?.setData(sma(bars, 20));
        series.sma50?.setData(sma(bars, 50));
        series.ema21?.setData(ema(bars, 21));
        if (series.bbU) { const b = bollinger(bars); series.bbU.setData(b.upper); series.bbL.setData(b.lower); }
        series.vwap?.setData(vwap(bars, rawTimes));
        series.rsi?.setData(rsi(bars));
        ref.current.last = bars.length ? { ...bars[bars.length - 1] } : null;
        if (first) {
          first = false;
          const n = bars.length;
          chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 4 });
          if (series.rsi) chart.panes()[1]?.setHeight(90);
        }
        setError("");
      } catch (e: any) {
        if (alive) setError(e.message);
      }
    };
    load();
    const timer = setInterval(load, 60000);
    return () => { alive = false; clearInterval(timer); chart.remove(); ref.current = {}; };
  }, [symbol, tf, inds.join(",")]);

  // Stream the latest price into the last candle.
  useEffect(() => {
    const { candles, last } = ref.current;
    if (!candles || !last || !quote || quote.symbol !== symbol) return;
    const price = quote.last;
    if (intraday) {
      const t = Math.floor(quote.ts / TF_SECONDS[tf]) * TF_SECONDS[tf] + shift;
      if (t > last.time) ref.current.last = { time: t, open: price, high: price, low: price, close: price };
      else if (t === last.time) Object.assign(last, { high: Math.max(last.high, price), low: Math.min(last.low, price), close: price });
      else return;
    } else {
      Object.assign(last, { high: Math.max(last.high, price), low: Math.min(last.low, price), close: price });
    }
    try { candles.update(ref.current.last); } catch {}
  }, [quote?.ts, quote?.last]);

  // Draw the position's average cost and open orders on the price scale.
  const lineKey = JSON.stringify([position?.avg_cost, position?.qty, orders.map((o) => [o.id, o.limit_price, o.stop_price, o.status])]);
  useEffect(() => {
    const { candles } = ref.current;
    if (!candles) return;
    const lines: any[] = [];
    if (position)
      lines.push(candles.createPriceLine({ price: position.avg_cost, color: "#4c9aff", lineWidth: 1, lineStyle: 0, title: `Avg cost (${position.qty})` }));
    for (const o of orders) {
      const price = o.limit_price ?? o.stop_price;
      if (price == null) continue;
      const label = `${o.side === "buy" ? "Buy" : "Sell"} ${o.remaining ?? o.qty - o.filled_qty} ${o.stop_price != null && !o.triggered ? "stop" : "limit"}`;
      lines.push(candles.createPriceLine({ price, color: o.side === "buy" ? COLORS.up : COLORS.down, lineWidth: 1, lineStyle: 2, title: label }));
    }
    return () => {
      if (ref.current.candles !== candles) return; // the chart was already removed; touching it would schedule a paint
      for (const l of lines) try { candles.removePriceLine(l); } catch {}
    };
  }, [lineKey, symbol, tf, inds.join(",")]);

  const toggle = (k: string) => {
    const next = inds.includes(k) ? inds.filter((i) => i !== k) : [...inds, k];
    setInds(next);
    store("pd.inds", next.join(","));
  };

  return (
    <div className="chart-wrap">
      <div className="chart-bar">
        <div className="seg" role="group" aria-label="Timeframe">
          {TFS.map((t) => (
            <button key={t} className={t === tf ? "on" : ""} onClick={() => { setTf(t); store("pd.tf", t); }}>{t.toUpperCase()}</button>
          ))}
        </div>
        <div className="seg" role="group" aria-label="Indicators">
          {Object.entries(INDICATORS).map(([k, label]) => (
            <button key={k} className={inds.includes(k) ? "on" : ""} disabled={k === "vwap" && !intraday}
              title={k === "vwap" && !intraday ? "VWAP is available on intraday timeframes" : undefined} onClick={() => toggle(k)}>{label}</button>
          ))}
        </div>
      </div>
      {error && <div className="notice bad">Could not load chart data: {error}</div>}
      <div className="chart" ref={el} role="figure" aria-label={`${symbol} price chart`} />
    </div>
  );
}
