import { useEffect, useRef, useState } from "react";
import { cleanParam, defaultParams, IndicatorConfig, IndicatorType, INDICATORS, indicatorLabel, newId } from "../indicators";
import { compact, money } from "../util";
import { Gen, plotRect, priceAt } from "./engine";

/** Re-render on every chart viewport change (scroll, zoom, resize, price scale change). */
export function useViewport(g: Gen | null) {
  const [, setN] = useState(0);
  useEffect(() => {
    if (!g) return;
    const f = () => setN((n) => n + 1);
    g.viewport.add(f);
    return () => { g.viewport.delete(f); };
  }, [g]);
}

const fmtVal = (v: number) => (!isFinite(v) ? "–" : Math.abs(v) >= 1e5 ? compact(v) : money(v, Math.abs(v) < 10 ? 3 : 2));

export function formatBarTime(g: Gen, raw: number) {
  const d = new Date(raw * 1000);
  return g.intraday
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
}

/** The crosshair legend: the hovered bar (or the latest one) and each indicator's value there. */
export function Legend({ g, tick }: { g: Gen | null; tick: unknown }) {
  const [hovered, setHovered] = useState<number | null>(null);
  useEffect(() => {
    if (!g) return;
    g.hover.add(setHovered);
    return () => { g.hover.delete(setHovered); };
  }, [g]);
  void tick;
  if (!g || !g.alive || !g.bars.length) return null;
  const i = hovered != null && hovered < g.bars.length ? hovered : g.bars.length - 1;
  const b = g.bars[i], prev = g.bars[i - 1];
  const ch = prev ? b.close - prev.close : null;
  const chp = prev && prev.close ? (ch! / prev.close) * 100 : null;
  const tone = ch == null || ch === 0 ? "" : ch > 0 ? "up" : "down";
  return (
    <div className="ct-legend" aria-live="off" data-testid="chart-legend">
      <div className="ct-legend-row">
        <b>{g.symbol}</b>
        <span>{formatBarTime(g, g.raw[i])}</span>
        <span>O <b>{money(b.open)}</b></span>
        <span>H <b>{money(b.high)}</b></span>
        <span>L <b>{money(b.low)}</b></span>
        <span>C <b className={tone}>{money(b.close)}</b></span>
        {ch != null && <span className={tone}>{ch > 0 ? "+" : ""}{money(ch)} ({chp! > 0 ? "+" : ""}{chp!.toFixed(2)}%)</span>}
        <span>Vol <b>{compact(b.volume)}</b></span>
      </div>
      <div className="ct-legend-row">
        {g.inds.map((ind) => (
          <span key={ind.cfg.id} title={ind.outputs.map((o) => o.label).join(", ")}>
            <i className="ct-swatch" style={{ background: ind.outputs[ind.outputs.length > 1 && ind.outputs[0].kind === "histogram" ? 1 : 0]?.color }} />
            {indicatorLabel(ind.cfg)}{" "}
            <b>{ind.outputs.map((o) => fmtVal(o.values[i])).join(" / ")}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

export type OrderLine = {
  key: string; oid: string; field: "limit_price" | "stop_price"; price: number; label: string; color: string; draggable: boolean;
};

/** Draggable handles for working orders' price lines. A drag only proposes a price; the parent asks to confirm. */
export function OrderHandles({ g, lines, draft, onDraft, onDrop }: {
  g: Gen | null; lines: OrderLine[]; draft: { key: string; price: number } | null;
  onDraft: (key: string, price: number | null) => void; onDrop: (line: OrderLine, price: number) => void;
}) {
  useViewport(g);
  const drag = useRef<{ key: string; dy: number; start: number; moved: boolean } | null>(null);
  if (!g || !g.alive || !g.loaded) return null;
  const rect = plotRect(g);
  return (
    <>
      {lines.filter((l) => l.draggable).map((l) => {
        const dragging = draft?.key === l.key;
        const price = dragging ? draft!.price : l.price;
        const raw = g.main.priceToCoordinate(price);
        if (raw == null) return null;
        // Off-screen lines get no handle, except the one being dragged: it stays inside the pane (unmounting it
        // mid-drag would lose the pointer).
        if (!dragging && (raw < 0 || raw > rect.height)) return null;
        const y = Math.max(10, Math.min(rect.height - 10, raw));
        const move = (clientY: number, el: HTMLElement) => {
          const d = drag.current;
          if (!d) return;
          const box = el.parentElement!.getBoundingClientRect();
          const yy = Math.max(10, Math.min(rect.height - 10, clientY - box.top - d.dy));
          const p = priceAt(g, yy);
          if (p != null) { d.moved = d.moved || Math.abs(clientY - d.start) > 2; onDraft(l.key, p); }
        };
        return (
          <button key={l.key} type="button" className="ct-order-handle" style={{ top: y, left: rect.left + 6, borderColor: l.color, color: l.color }}
            aria-label={`${l.label} at ${money(price)}. Drag, or use the arrow keys, to change the price`}
            onPointerDown={(e) => {
              e.preventDefault();
              e.currentTarget.setPointerCapture(e.pointerId);
              const box = e.currentTarget.parentElement!.getBoundingClientRect();
              drag.current = { key: l.key, dy: e.clientY - box.top - y, start: e.clientY, moved: false };
            }}
            onPointerMove={(e) => move(e.clientY, e.currentTarget)}
            onPointerUp={() => {
              const d = drag.current;
              drag.current = null;
              if (d && d.moved && draft?.key === l.key && draft.price !== l.price) onDrop(l, draft.price);
              else onDraft(l.key, null);
            }}
            onLostPointerCapture={() => { if (drag.current) { drag.current = null; onDraft(l.key, null); } }}
            onPointerCancel={() => { drag.current = null; onDraft(l.key, null); }}
            onKeyDown={(e) => {
              const stepSize = e.shiftKey ? 0.1 : 0.01;
              if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                e.preventDefault();
                const next = Math.max(0.01, Math.round((price + (e.key === "ArrowUp" ? stepSize : -stepSize)) * 100) / 100);
                onDraft(l.key, next);
              } else if (e.key === "Enter" && draft?.key === l.key && draft.price !== l.price) {
                e.preventDefault(); onDrop(l, draft.price);
              } else if (e.key === "Escape" && draft?.key === l.key) { e.preventDefault(); onDraft(l.key, null); }
            }}>
            <span aria-hidden="true">⇕</span> {l.label} {money(price)}
          </button>
        );
      })}
    </>
  );
}

export function Popover({ x, y, label, role = "dialog", onClose, children }: {
  x: number; y: number; label: string; role?: string; onClose: () => void; children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });
  useEffect(() => {
    // Keep the popover inside the chart.
    const el = ref.current, parent = el?.offsetParent as HTMLElement | null;
    if (!el || !parent) return;
    setPos({ x: Math.max(4, Math.min(x, parent.clientWidth - el.offsetWidth - 4)), y: Math.max(4, Math.min(y, parent.clientHeight - el.offsetHeight - 4)) });
  }, [x, y]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    const down = (e: PointerEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose(); };
    window.addEventListener("keydown", key, true);
    const t = setTimeout(() => window.addEventListener("pointerdown", down, true), 0);
    return () => { window.removeEventListener("keydown", key, true); clearTimeout(t); window.removeEventListener("pointerdown", down, true); };
  }, [onClose]);
  return (
    <div ref={ref} className="ct-popover" role={role} aria-label={label} style={{ left: pos.x, top: pos.y }}>
      {children}
    </div>
  );
}

export function IndicatorPanel({ list, intraday, onChange, onClose }: {
  list: IndicatorConfig[]; intraday: boolean; onChange: (l: IndicatorConfig[]) => void; onClose: () => void;
}) {
  const add = (type: string) => {
    if (!(type in INDICATORS)) return;
    onChange([...list, { id: newId(), type: type as IndicatorType, params: defaultParams(type as IndicatorType) }]);
  };
  return (
    <div className="ct-panel" role="dialog" aria-label="Indicators">
      <div className="ct-panel-head">
        <b>Indicators</b>
        <button type="button" className="ct-btn" onClick={onClose} aria-label="Close indicators">×</button>
      </div>
      {list.length === 0 && <p className="muted">No indicators. Add one below.</p>}
      {list.map((c) => {
        const def = INDICATORS[c.type];
        return (
          <div className="ct-ind-row" key={c.id}>
            <span className="ct-ind-name">{def.name}{def.intradayOnly && !intraday ? <span className="muted"> (intraday timeframes only)</span> : null}</span>
            {def.params.map((s) => (
              <ParamInput key={s.key} label={`${def.name} ${s.label.toLowerCase()}`} short={s.label} value={c.params[s.key]}
                step={s.step} onCommit={(raw) => {
                  const v = cleanParam(s, raw);
                  if (v == null || v === c.params[s.key]) return;
                  onChange(list.map((x) => (x.id === c.id ? { ...x, params: { ...x.params, [s.key]: v } } : x)));
                }} />
            ))}
            <button type="button" className="ct-btn" aria-label={`Remove ${indicatorLabel(c)}`} title="Remove"
              onClick={() => onChange(list.filter((x) => x.id !== c.id))}>×</button>
          </div>
        );
      })}
      <select aria-label="Add indicator" value="" onChange={(e) => add(e.target.value)}>
        <option value="">Add indicator…</option>
        {Object.entries(INDICATORS).map(([k, d]) => (
          <option key={k} value={k}>{d.name}{d.place === "pane" ? " (own pane)" : ""}{d.intradayOnly ? " (intraday)" : ""}</option>
        ))}
      </select>
    </div>
  );
}

function ParamInput({ label, short, value, step, onCommit }: { label: string; short: string; value: number; step?: number; onCommit: (raw: string) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return (
    <label className="ct-param">
      <span>{short}</span>
      <input type="number" inputMode="decimal" aria-label={label} value={draft} step={step ?? 1}
        onChange={(e) => { setDraft(e.target.value); onCommit(e.target.value); }}
        onBlur={() => setDraft(String(value))} />
    </label>
  );
}
