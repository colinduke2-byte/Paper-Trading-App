import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { IPriceLine, LogicalRange } from "lightweight-charts";
import { api, money, store } from "./util";
import type { Bar } from "./indicators";
import { Anchor, describeDrawing, Drawing, DRAWING_NAMES, DrawingType, MAX_DRAWINGS, MAX_TEXT, newDrawingId, XY } from "./chart/drawings";
import { create, defaultView, sizePanes, destroy, fromPx, Gen, INTRADAY, plotRect, priceAt, scaleMode, setBars, setCompare, streamPrice, toPx } from "./chart/engine";
import { DrawLayer, SceneState, Tool } from "./chart/DrawLayer";
import { formatBarTime, IndicatorPanel, Legend, OrderHandles, OrderLine, Popover } from "./chart/parts";
import { CHART_TYPES, ChartType, useChartSettings, useDrawings } from "./chart/state";
import "./chart/chart.css";

const TFS = ["1m", "5m", "15m", "1h", "1d", "1w"];
const TOOLS: DrawingType[] = ["trend", "ray", "hline", "vline", "rect", "fib", "text"];
const TOOL_SHORT: Record<DrawingType, string> = { trend: "Trend", ray: "Ray", hline: "H-line", vline: "V-line", rect: "Rect", fib: "Fib", text: "Text" };
export const TIMEFRAME_EVENT = "pd:timeframe";
export const SHORTCUTS_EVENT = "pd:shortcuts";

export type TradeSeed = { symbol: string; side: "buy" | "sell"; type: "limit" | "stop"; limit_price?: number; stop_price?: number };

const isTyping = (t: EventTarget | null) => {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
};

export function Chart({ symbol, quote, position, orders, accountId, notify, onOrdersChanged, onTradeAtPrice, scope = "live" }: {
  symbol: string; quote: any; position: any; orders: any[];
  /** Where drawings are kept: "live" or "replay:<session id>". The chart is remounted when it changes. */
  scope?: string;
  accountId?: string; notify?: (text: string, bad?: boolean) => void; onOrdersChanged?: () => void; onTradeAtPrice?: (s: TradeSeed) => void;
}) {
  const el = useRef<HTMLDivElement>(null);
  const [tf, setTfState] = useState(() => { const t = store("pd.tf"); return t && TFS.includes(t) ? t : "5m"; });
  const say = useCallback((m: string, bad = true) => notify?.(m, bad), [notify]);
  const [settings, setSettings] = useChartSettings(say);
  const settingsRef = useRef(settings);
  settingsRef.current = settings;
  const [drawings, setDrawings] = useDrawings(symbol, scope, say);
  const [error, setError] = useState("");
  const [compareError, setCompareError] = useState("");
  const [g, setG] = useState<Gen | null>(null);
  const [loadTick, setLoadTick] = useState(0);
  const [panel, setPanel] = useState<null | "indicators" | "drawings">(null);
  const [drawMode, setDrawMode] = useState(false);
  const [tool, setTool] = useState<Tool>("select");
  const [selected, setSelected] = useState<string | null>(null);
  const [menu, setMenu] = useState<null | { at: XY; price: number }>(null);
  const [textAt, setTextAt] = useState<null | { at: XY; anchor: Anchor; value: string }>(null);
  const [draft, setDraft] = useState<null | { key: string; price: number }>(null);
  const [confirm, setConfirm] = useState<null | { line: OrderLine; from: number; to: number; busy?: boolean; error?: string }>(null);
  const [compareInput, setCompareInput] = useState("");
  const intraday = INTRADAY.has(tf);
  const cache = useRef(new Map<string, Bar[]>());
  const view = useRef<{ key: string; range: LogicalRange | null } | null>(null);
  const scene = useRef<SceneState>({ drawings, draft: null, preview: null, selected: null });
  scene.current.drawings = drawings;
  scene.current.selected = drawMode ? selected : null;

  const setTf = (t: string) => { if (TFS.includes(t)) { setTfState(t); store("pd.tf", t); } };
  const indKey = JSON.stringify(settings.indicators.map((c) => [c.type, c.params, c.id]));

  // ---- chart lifecycle ---------------------------------------------------------------------------------
  useEffect(() => {
    if (!el.current) return;
    const gen = create(el.current, {
      symbol, tf, type: settings.type, indicators: settings.indicators, compare: settings.compare, log: settings.log,
      scene: (gg) => ({ ...scene.current, drawings: scene.current.draft ?? scene.current.drawings, toPx: (a) => toPx(gg, a), fmtPrice: (p) => money(p) }),
    });
    setG(gen);
    let first = true;
    const render = (raw: Bar[]) => {
      if (!gen.alive) return;
      setBars(gen, raw);
      if (first) {
        first = false;
        const v = view.current;
        if (v && v.key === gen.key && v.range) gen.chart.timeScale().setVisibleLogicalRange(v.range);
        else defaultView(gen);
        sizePanes(gen);
      }
      setLoadTick((n) => n + 1);
    };
    const cached = cache.current.get(gen.key);
    if (cached) render(cached);
    const load = async () => {
      try {
        const raw: Bar[] = await api(`/bars/${symbol}?tf=${tf}&limit=${intraday ? 1500 : 520}`);
        if (!gen.alive) return;
        cache.current.set(gen.key, raw);
        if (cache.current.size > 12) cache.current.delete(cache.current.keys().next().value as string);
        render(raw);
        setError("");
      } catch (e: any) {
        if (gen.alive) setError(e.message);
      }
      if (gen.compare && gen.alive) {
        try {
          const c: Bar[] = await api(`/bars/${gen.compareSymbol}?tf=${tf}&limit=${intraday ? 1500 : 520}`);
          setCompare(gen, c);
          if (gen.alive) setCompareError("");
        } catch (e: any) {
          if (gen.alive) setCompareError(`Could not load ${gen.compareSymbol} to compare: ${e.message}`);
        }
      }
    };
    load();
    const timer = setInterval(load, 60000);
    return () => {
      clearInterval(timer);
      if (gen.alive) view.current = { key: gen.key, range: gen.chart.timeScale().getVisibleLogicalRange() };
      destroy(gen);
      setG((cur) => (cur === gen ? null : cur));
    };
  }, [symbol, tf, settings.type, indKey, settings.compare]);

  useEffect(() => {
    if (!g || !g.alive) return;
    g.main.priceScale().applyOptions({ mode: scaleMode(settings.log, !!settings.compare) });
  }, [g, settings.log, settings.compare]);

  useEffect(() => { if (g?.alive) g.primitive.redraw(); }, [g, drawings, selected, drawMode]);
  useEffect(() => { setSelected(null); setMenu(null); setTextAt(null); }, [symbol]);

  // Stream the latest price into the last bar and every indicator.
  useEffect(() => {
    if (!g || !quote || quote.symbol !== symbol) return;
    streamPrice(g, quote.last, quote.ts);
  }, [g, quote?.ts, quote?.last]);

  // ---- position and order lines -------------------------------------------------------------------------
  const lines: OrderLine[] = useMemo(() => {
    const out: OrderLine[] = [];
    for (const o of orders) {
      // A multi-leg order's price is a net debit or credit per unit, not a price of this symbol: no line.
      if (Array.isArray(o.legs) && o.legs.length) continue;
      if (!["working", "partially_filled"].includes(o.status)) continue;
      const rem = o.remaining ?? o.qty - o.filled_qty;
      const side = o.side === "buy" ? "Buy" : "Sell";
      const tag = o.role === "take_profit" ? "Take profit: " : o.role === "stop_loss" ? "Stop loss: " : "";
      const color = o.role === "take_profit" ? "#26c6da" : o.role === "stop_loss" ? "#ff9800" : o.side === "buy" ? "#2fbf71" : "#ef5350";
      const stopLive = Number.isFinite(o.stop_price) && !o.triggered;
      if (stopLive) out.push({ key: `${o.id}:stop`, oid: o.id, field: "stop_price", price: o.stop_price, color,
        label: `${tag}${side} ${rem} ${o.type === "trailing_stop" ? "trailing stop" : "stop"}`, draggable: o.type === "stop" || o.type === "stop_limit" });
      if (Number.isFinite(o.limit_price)) out.push({ key: `${o.id}:limit`, oid: o.id, field: "limit_price", price: o.limit_price, color,
        label: `${tag}${side} ${rem} limit`, draggable: o.type === "limit" || o.type === "stop_limit" });
    }
    return out;
  }, [JSON.stringify(orders.map((o) => [o.id, o.limit_price, o.stop_price, o.status, o.triggered, o.role, o.remaining, o.qty, o.filled_qty, o.type, o.side]))]);
  const priceLines = useRef(new Map<string, IPriceLine>());
  const lineKey = JSON.stringify([position?.avg_cost, position?.qty, lines]);
  // Keep working orders near the market (and the average cost) on screen by including them in the autoscale.
  // Prices more than 15% away are left out so a far-off order does not flatten the chart.
  const scalePrices = useRef<number[]>([]);
  const refPrice = quote?.symbol === symbol ? quote.last : null;
  scalePrices.current = [...lines.map((l) => l.price), ...(position ? [position.avg_cost] : [])]
    .filter((p) => !refPrice || Math.abs(p / refPrice - 1) <= 0.15);
  useEffect(() => { if (g?.alive) { g.primitive.extraPrices = () => scalePrices.current; g.primitive.redraw(); } }, [g, lineKey]);
  useEffect(() => {
    if (!g || !g.alive) return;
    const main = g.main;
    const made: IPriceLine[] = [];
    const map = new Map<string, IPriceLine>();
    if (position)
      made.push(main.createPriceLine({ price: position.avg_cost, color: "#4c9aff", lineWidth: 1, lineStyle: 0, title: `Avg cost (${position.qty})` }));
    for (const l of lines) {
      const bracket = l.label.startsWith("Take profit") || l.label.startsWith("Stop loss");
      const pl = main.createPriceLine({ price: l.price, color: l.color, lineWidth: bracket ? 2 : 1, lineStyle: bracket ? 3 : 2,
        title: l.label.replace("Take profit: ", "TP ").replace("Stop loss: ", "SL ") });
      made.push(pl); map.set(l.key, pl);
    }
    priceLines.current = map;
    return () => {
      if (!g.alive) return; // the chart was already removed; touching it would schedule a paint
      for (const pl of made) try { main.removePriceLine(pl); } catch {}
    };
  }, [g, lineKey]);
  // Apply a dragged (not yet confirmed) price to its line.
  useEffect(() => {
    if (!g?.alive) return;
    for (const l of lines) {
      const pl = priceLines.current.get(l.key);
      const want = draft?.key === l.key ? draft.price : confirm?.line.key === l.key ? confirm.to : l.price;
      try { pl?.applyOptions({ price: want }); } catch {}
    }
  }, [g, draft, confirm, lineKey]);

  const sendChange = async () => {
    if (!confirm || !accountId) return;
    setConfirm({ ...confirm, busy: true, error: undefined });
    try {
      await api(`/accounts/${accountId}/orders/${confirm.line.oid}`, { method: "PATCH", body: { [confirm.line.field]: confirm.to.toFixed(2) } });
      notify?.(`Order changed: ${confirm.line.label} now at ${money(confirm.to)}`, false);
      setConfirm(null); setDraft(null);
      onOrdersChanged?.();
    } catch (e: any) {
      // Refused (buying power, a day-trading rule, a filled or canceled order...): say why and put the line back.
      setConfirm(null); setDraft(null);
      notify?.(`The order was not changed: ${e.message}`, true);
      onOrdersChanged?.();
    }
  };

  // ---- trade at price ------------------------------------------------------------------------------------
  const openMenu = (at: XY) => {
    if (!g?.alive) return;
    const r = plotRect(g);
    if (at.x < r.left || at.x > r.left + r.width || at.y < 0 || at.y > r.height) return;
    const price = priceAt(g, at.y);
    if (price != null) setMenu({ at, price });
  };
  const longPress = useRef<{ timer: any; at: XY; fired: boolean } | null>(null);
  const stagePointerDown = (e: React.PointerEvent) => {
    if (e.pointerType !== "touch" || drawMode || !el.current) return;
    const r = el.current.getBoundingClientRect();
    const at = { x: e.clientX - r.left, y: e.clientY - r.top };
    clearTimeout(longPress.current?.timer);
    longPress.current = { at, fired: false, timer: setTimeout(() => { if (longPress.current) longPress.current.fired = true; openMenu(at); }, 600) };
  };
  const stagePointerMove = (e: React.PointerEvent) => {
    const lp = longPress.current;
    if (!lp || !el.current) return;
    const r = el.current.getBoundingClientRect();
    if (Math.hypot(e.clientX - r.left - lp.at.x, e.clientY - r.top - lp.at.y) > 10) { clearTimeout(lp.timer); longPress.current = null; }
  };
  const stagePointerEnd = () => { if (longPress.current) clearTimeout(longPress.current.timer); };
  const stageContextMenu = (e: React.MouseEvent) => {
    if (!el.current || !el.current.contains(e.target as Node)) return;
    e.preventDefault();
    if (longPress.current?.fired) return; // the long-press already opened the menu
    const r = el.current.getBoundingClientRect();
    openMenu({ x: e.clientX - r.left, y: e.clientY - r.top });
  };
  const trade = (side: "buy" | "sell", type: "limit" | "stop", price: number) => {
    setMenu(null);
    onTradeAtPrice?.({ symbol, side, type, ...(type === "limit" ? { limit_price: price } : { stop_price: price }) });
    notify?.(`Order ticket filled in: ${side} ${type} at ${money(price)}. Enter a quantity and review it to place the order.`, false);
    // In the single-column (small screen) layout the ticket is further down the page: bring it into view.
    if (window.matchMedia?.("(max-width: 1100px)").matches)
      document.querySelector('section[aria-label="Order ticket"]')?.scrollIntoView({ block: "start", behavior: "smooth" });
  };

  // ---- drawings -----------------------------------------------------------------------------------------
  const addDrawing = (d: Drawing) => {
    if (drawings.length >= MAX_DRAWINGS) { say(`A chart holds at most ${MAX_DRAWINGS} drawings; remove some first.`); return; }
    setDrawings([...drawings, d]);
  };
  const removeDrawing = (id: string) => { setDrawings(drawings.filter((d) => d.id !== id)); if (selected === id) setSelected(null); };
  const fmtTime = (t: number) => (g ? formatBarTime(g, t) : new Date(t * 1000).toLocaleString());

  // Chart keys: Delete removes the selected drawing, Escape leaves the current tool.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (drawMode && selected && (e.key === "Delete" || e.key === "Backspace")) { e.preventDefault(); removeDrawing(selected); }
      else if (drawMode && e.key === "Escape") { if (tool !== "select") setTool("select"); else if (selected) setSelected(null); else setDrawMode(false); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });
  useEffect(() => {
    const onTf = (e: Event) => setTf(String((e as CustomEvent).detail));
    window.addEventListener(TIMEFRAME_EVENT, onTf);
    return () => window.removeEventListener(TIMEFRAME_EVENT, onTf);
  }, []);

  const submitCompare = async () => {
    const s = compareInput.trim().toUpperCase();
    if (!/^[A-Z0-9.\-]{1,24}$/.test(s)) { setCompareError("Enter a symbol to compare with, such as TECX."); return; }
    if (s === symbol) { setCompareError(`The chart already shows ${symbol}; choose another symbol to compare.`); return; }
    try {
      const found: any[] = await api(`/symbols?q=${encodeURIComponent(s)}`);
      if (!found.some((r) => r.symbol === s)) { setCompareError(`There is no symbol ${s} to compare with.`); return; }
    } catch (e: any) { setCompareError(`Could not look up ${s}: ${e.message}`); return; }
    setCompareError("");
    setSettings({ ...settingsRef.current, compare: s });
    setCompareInput("");
  };

  const resetView = () => { if (g?.alive) defaultView(g); };
  const selectedDrawing = drawings.find((d) => d.id === selected) || null;
  const lastPrice = quote?.symbol === symbol ? quote.last : g?.bars[g.bars.length - 1]?.close;

  return (
    <div className="chart-wrap ct-wrap">
      <div className="ct-bar" role="toolbar" aria-label="Chart tools">
        <div className="ct-seg" role="group" aria-label="Timeframe">
          {TFS.map((t) => <button key={t} type="button" className={t === tf ? "on" : ""} aria-pressed={t === tf} onClick={() => setTf(t)}>{t.toUpperCase()}</button>)}
        </div>
        <select className="ct-select" aria-label="Chart type" value={settings.type} onChange={(e) => setSettings({ ...settings, type: e.target.value as ChartType })}>
          {Object.entries(CHART_TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button type="button" className={"ct-btn" + (settings.log && !settings.compare ? " on" : "")} aria-pressed={settings.log && !settings.compare}
          disabled={!!settings.compare} title={settings.compare ? "The scale shows percentage change while comparing" : "Logarithmic price scale"}
          onClick={() => setSettings({ ...settings, log: !settings.log })}>Log</button>
        <button type="button" className={"ct-btn" + (panel === "indicators" ? " on" : "")} aria-expanded={panel === "indicators"}
          onClick={() => setPanel(panel === "indicators" ? null : "indicators")}>Indicators ({settings.indicators.length})</button>
        {settings.compare ? (
          <span className="ct-chip">vs {settings.compare}
            <button type="button" className="ct-btn" aria-label={`Stop comparing with ${settings.compare}`} onClick={() => { setSettings({ ...settings, compare: "" }); setCompareError(""); }}>×</button>
          </span>
        ) : (
          <form className="ct-compare" onSubmit={(e) => { e.preventDefault(); submitCompare(); }}>
            <input aria-label="Compare with symbol" placeholder="Compare…" value={compareInput} onChange={(e) => setCompareInput(e.target.value)} />
            <button type="submit" className="ct-btn">Add</button>
          </form>
        )}
        <button type="button" className={"ct-btn" + (drawMode ? " on" : "")} aria-pressed={drawMode}
          title="Draw mode: tap-drag draws instead of panning the chart" onClick={() => { setDrawMode(!drawMode); setTool("select"); setSelected(null); }}>Draw</button>
        <button type="button" className="ct-btn" onClick={resetView} aria-label="Reset chart view" title="Reset chart view">Reset view</button>
      </div>
      {drawMode && (
        <div className="ct-bar ct-draw-bar" role="toolbar" aria-label="Drawing tools">
          <div className="ct-seg" role="group" aria-label="Drawing tool">
            <button type="button" className={tool === "select" ? "on" : ""} aria-pressed={tool === "select"} onClick={() => setTool("select")}>Select</button>
            {TOOLS.map((t) => (
              <button key={t} type="button" className={tool === t ? "on" : ""} aria-pressed={tool === t} aria-label={DRAWING_NAMES[t]} title={DRAWING_NAMES[t]}
                onClick={() => { setTool(t); setSelected(null); }}>{TOOL_SHORT[t]}</button>
            ))}
          </div>
          <button type="button" className="ct-btn" disabled={!selectedDrawing} onClick={() => selected && removeDrawing(selected)}>Delete selected</button>
          <button type="button" className={"ct-btn" + (panel === "drawings" ? " on" : "")} aria-expanded={panel === "drawings"}
            onClick={() => setPanel(panel === "drawings" ? null : "drawings")}>Drawings ({drawings.length})</button>
          <button type="button" className="ct-btn" onClick={() => { setDrawMode(false); setSelected(null); setTool("select"); if (panel === "drawings") setPanel(null); }}>Done</button>
          <span className="muted ct-hint">{tool === "select" ? "Tap a drawing to select it, drag to move it." : POINTS_HINT[tool]}</span>
        </div>
      )}
      <div className="ct-credit">
        <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">Charting by TradingView</a>
      </div>
      {error && <div className="notice bad">Could not load chart data: {error}</div>}
      {compareError && <div className="notice bad">{compareError}</div>}
      <div className="ct-stage" data-chart-type={settings.type} data-log={settings.log ? "1" : "0"} data-loaded={loadTick > 0 ? "1" : "0"}
        onPointerDown={stagePointerDown} onPointerMove={stagePointerMove} onPointerUp={stagePointerEnd} onPointerCancel={stagePointerEnd}
        onContextMenu={stageContextMenu}>
        <div className="chart ct-host" ref={el} role="img" aria-label={`${symbol} price chart`} />
        <Legend g={g} tick={[loadTick, quote?.ts]} />
        <OrderHandles g={g} lines={lines} draft={confirm ? { key: confirm.line.key, price: confirm.to } : draft}
          onDraft={(key, price) => { if (!confirm) setDraft(price == null ? null : { key, price }); }}
          onDrop={(line, to) => { setDraft(null); setConfirm({ line, from: line.price, to }); }} />
        {drawMode && g && (
          <DrawLayer g={g} scene={scene} tool={tool} setTool={setTool} drawings={drawings} setDrawings={(d) => d.length <= MAX_DRAWINGS ? setDrawings(d) : say(`A chart holds at most ${MAX_DRAWINGS} drawings; remove some first.`)}
            setSelected={setSelected} onText={(at, anchor) => setTextAt({ at, anchor, value: "" })} onMenu={openMenu} />
        )}
        {panel === "indicators" && (
          <IndicatorPanel list={settings.indicators} intraday={intraday} onChange={(l) => setSettings({ ...settings, indicators: l })} onClose={() => setPanel(null)} />
        )}
        {panel === "drawings" && (
          <div className="ct-panel" role="dialog" aria-label="Drawings">
            <div className="ct-panel-head"><b>Drawings on {symbol}</b><button type="button" className="ct-btn" aria-label="Close drawings" onClick={() => setPanel(null)}>×</button></div>
            {drawings.length === 0 && <p className="muted">No drawings on this chart.</p>}
            <ul className="ct-list">
              {drawings.map((d) => {
                const text = describeDrawing(d, fmtTime, (p) => money(p));
                return (
                  <li key={d.id} className={d.id === selected ? "sel" : ""}>
                    <button type="button" className="ct-link" onClick={() => { setDrawMode(true); setSelected(d.id); }}>{text}</button>
                    <button type="button" className="ct-btn" aria-label={`Remove ${text}`} onClick={() => removeDrawing(d.id)}>×</button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {menu && (
          <Popover x={menu.at.x} y={menu.at.y} label={`Trade at ${money(menu.price)}`} role="menu" onClose={() => setMenu(null)}>
            <div className="ct-pop-title">At {money(menu.price)}</div>
            {lastPrice == null || menu.price <= lastPrice ? (
              <>
                <button type="button" role="menuitem" className="ct-menu-item up" onClick={() => trade("buy", "limit", menu.price)}>Buy limit at {money(menu.price)}</button>
                <button type="button" role="menuitem" className="ct-menu-item down" onClick={() => trade("sell", "stop", menu.price)}>Sell stop at {money(menu.price)}</button>
              </>
            ) : (
              <>
                <button type="button" role="menuitem" className="ct-menu-item down" onClick={() => trade("sell", "limit", menu.price)}>Sell limit at {money(menu.price)}</button>
                <button type="button" role="menuitem" className="ct-menu-item up" onClick={() => trade("buy", "stop", menu.price)}>Buy stop at {money(menu.price)}</button>
              </>
            )}
            <button type="button" role="menuitem" className="ct-menu-item" onClick={() => {
              const a = g && fromPx(g, menu.at);
              setMenu(null);
              if (a) addDrawing({ id: newDrawingId(), type: "hline", points: [{ t: a.t, p: menu.price }] });
            }}>Horizontal line at {money(menu.price)}</button>
          </Popover>
        )}
        {textAt && (
          <Popover x={textAt.at.x} y={textAt.at.y} label="Add a text note" onClose={() => setTextAt(null)}>
            <form onSubmit={(e) => {
              e.preventDefault();
              const t = textAt.value.trim().slice(0, MAX_TEXT);
              if (t) { const d = { id: newDrawingId(), type: "text" as const, points: [textAt.anchor], text: t }; addDrawing(d); setSelected(d.id); }
              setTextAt(null); setTool("select");
            }}>
              <input autoFocus aria-label="Note text" maxLength={MAX_TEXT} value={textAt.value} onChange={(e) => setTextAt({ ...textAt, value: e.target.value })} />
              <div className="ct-row">
                <button type="button" className="ct-btn" onClick={() => setTextAt(null)}>Cancel</button>
                <button type="submit" className="ct-btn primary">Add note</button>
              </div>
            </form>
          </Popover>
        )}
        {confirm && (
          <div className="ct-confirm" role="dialog" aria-label="Confirm order change">
            <p>Change <b>{confirm.line.label}</b> {confirm.line.field === "stop_price" ? "stop price" : "limit price"} from <b>{money(confirm.from)}</b> to <b>{money(confirm.to)}</b>?</p>
            {confirm.error && <div className="notice bad" role="alert">{confirm.error}</div>}
            <div className="ct-row">
              <button type="button" className="ct-btn" disabled={confirm.busy} onClick={() => { setConfirm(null); setDraft(null); }}>Cancel</button>
              <button type="button" className="ct-btn primary" disabled={confirm.busy || !accountId} onClick={sendChange}>Change order</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const POINTS_HINT: Record<DrawingType, string> = {
  trend: "Drag from the first point to the second, or tap twice.", ray: "Drag from the start through a second point, or tap twice.",
  hline: "Tap at the price.", vline: "Tap at the time.", rect: "Drag from one corner to the opposite corner, or tap twice.",
  fib: "Drag from the start of the move to its end, or tap twice.", text: "Tap where the note goes.",
};
