import { useEffect, useRef, useState } from "react";
import { usePoll } from "./Market";
import { Spark } from "./Spark";
import { compact, dateTime, money, pct, tone } from "./util";
import "./discovery.css";

// Market overview: index and fund tiles, breadth, a sector heatmap and the top movers. Registered as a
// view tab next to Chart/Options/Company/Research because it needs the large centre area (the heatmap)
// and, like those views, ends in a symbol: picking anything opens that symbol's chart.

type Rect = { x: number; y: number; w: number; h: number };

/** Squarified treemap of items (largest first) into the rectangle; areas are proportional to size. */
export function treemap<T extends { size: number }>(items: T[], r: Rect): (T & Rect)[] {
  const total = items.reduce((a, b) => a + Math.max(b.size || 0, 0), 0);
  if (!items.length || total <= 0 || r.w <= 0 || r.h <= 0) return [];
  const scale = (r.w * r.h) / total;
  const nodes = items.map((it) => ({ it, area: Math.max(it.size || 0, 0) * scale })).filter((n) => n.area > 0)
    .sort((a, b) => b.area - a.area);
  const out: (T & Rect)[] = [];
  let { x, y, w, h } = r;
  const worst = (row: typeof nodes, side: number) => {
    const s = row.reduce((a, n) => a + n.area, 0);
    const max = Math.max(...row.map((n) => n.area)), min = Math.min(...row.map((n) => n.area));
    return Math.max((side * side * max) / (s * s), (s * s) / (side * side * min));
  };
  const place = (row: typeof nodes) => {
    const s = row.reduce((a, n) => a + n.area, 0);
    if (w >= h) {
      const cw = s / h; let cy = y;
      for (const n of row) { const ch = n.area / cw; out.push({ ...n.it, x, y: cy, w: cw, h: ch }); cy += ch; }
      x += cw; w -= cw;
    } else {
      const rh = s / w; let cx = x;
      for (const n of row) { const cw = n.area / rh; out.push({ ...n.it, x: cx, y, w: cw, h: rh }); cx += cw; }
      y += rh; h -= rh;
    }
  };
  let row: typeof nodes = [];
  for (let i = 0; i < nodes.length;) {
    const side = Math.min(w, h);
    if (!row.length || worst([...row, nodes[i]], side) <= worst(row, side)) { row.push(nodes[i]); i++; }
    else { place(row); row = []; }
  }
  if (row.length) place(row);
  return out;
}

/** Red for falls, green for rises, grey near zero; full colour at ±3 %. */
export function heat(change: number | null | undefined): string {
  if (change == null || !isFinite(change)) return "rgb(42,48,56)";
  const t = Math.max(-1, Math.min(1, change / 3));
  const base = [42, 48, 56], to = t >= 0 ? [27, 140, 80] : [190, 50, 48];
  const k = Math.abs(t);
  return `rgb(${base.map((b, i) => Math.round(b + (to[i] - b) * k)).join(",")})`;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => setWidth(e[0].contentRect.width));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function Heatmap({ sectors, onSelect }: { sectors: any[]; onSelect: (s: string) => void }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const W = 1000, H = 560, HEAD = 22;
  const boxes = treemap(sectors.map((s) => ({ ...s, size: s.size })), { x: 0, y: 0, w: W, h: H });
  const pctX = (v: number) => `${(v / W) * 100}%`, pctY = (v: number) => `${(v / H) * 100}%`;
  return (
    <div className="hm" ref={ref} role="group" aria-label="Sector heatmap">
      {width > 0 && boxes.map((b) => {
        const tiles = treemap(b.members, { x: b.x + 1, y: b.y + HEAD, w: Math.max(b.w - 2, 0), h: Math.max(b.h - HEAD - 1, 0) });
        return (
          <div key={b.sector}>
            <div className="hm-sector" style={{ left: pctX(b.x), top: pctY(b.y), width: pctX(b.w), height: pctY(b.h) }}>
              <span className="hm-sector-name">{b.sector} <span className={tone(b.change_pct)}>{pct(b.change_pct)}</span></span>
            </div>
            {tiles.map((t: any) => {
              const px = (t.w / W) * width, py = (t.h / H) * (width * H / W);
              return (
                <button key={t.symbol} className="hm-tile" style={{ left: pctX(t.x), top: pctY(t.y), width: pctX(t.w), height: pctY(t.h), background: heat(t.change_pct) }}
                  title={`${t.symbol} · ${t.name} · ${pct(t.change_pct)}`} aria-label={`${t.symbol} ${pct(t.change_pct)}`} onClick={() => onSelect(t.symbol)}>
                  {px > 34 && py > 18 && <b>{t.symbol}</b>}
                  {px > 48 && py > 34 && <span>{pct(t.change_pct)}</span>}
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

// The phone fallback: sectors ranked by change, each with its members as tappable chips.
function SectorList({ sectors, onSelect }: { sectors: any[]; onSelect: (s: string) => void }) {
  const ranked = [...sectors].sort((a, b) => b.change_pct - a.change_pct);
  return (
    <ol className="hm-list" aria-label="Sectors ranked by change">
      {ranked.map((s) => (
        <li key={s.sector}>
          <div className="hm-list-head"><b>{s.sector}</b><span className={tone(s.change_pct)}>{pct(s.change_pct)}</span>
            <span className="muted">{compact(s.size)}</span></div>
          <div className="hm-chips">
            {s.members.map((m: any) => (
              <button key={m.symbol} className="hm-chip" style={{ background: heat(m.change_pct) }} aria-label={`${m.symbol} ${pct(m.change_pct)}`}
                onClick={() => onSelect(m.symbol)}><b>{m.symbol}</b> {pct(m.change_pct)}</button>
            ))}
          </div>
        </li>
      ))}
    </ol>
  );
}

function MoverTable({ title, rows, onSelect, volume }: { title: string; rows: any[]; onSelect: (s: string) => void; volume?: boolean }) {
  return (
    <div className="mk-movers" role="region" aria-label={title}>
      <h3>{title}</h3>
      <table className="tight">
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className="click" onClick={() => onSelect(r.symbol)}>
              <td><button className="link" onClick={(e) => { e.stopPropagation(); onSelect(r.symbol); }}>{r.symbol}</button></td>
              <td className="muted mk-name">{r.name}</td>
              <td className="num">{money(r.last)}</td>
              <td className={"num " + tone(r.change_pct)}>{pct(r.change_pct)}</td>
              {volume && <td className="num muted">{compact(r.dollar_volume)}</td>}
            </tr>
          ))}
          {rows.length === 0 && <tr><td className="muted">Nothing to show yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function Markets({ onSelect, small }: { onSelect: (s: string) => void; small: boolean }) {
  const [o, error] = usePoll<any>("/markets/overview?limit=10", 5000);
  const [map, setMap] = useState<"map" | "list">(small ? "list" : "map");
  const [movers, setMovers] = useState<"gainers" | "losers" | "active">("gainers");
  useEffect(() => { setMap(small ? "list" : "map"); }, [small]);
  if (error && !o) return <div className="notice bad">Could not load the market overview: {error}</div>;
  if (!o) return <p className="muted pad">Loading the market overview…</p>;
  const b = o.breadth;
  const adv = b.total ? (b.advancers / b.total) * 100 : 0, dec = b.total ? (b.decliners / b.total) * 100 : 0;
  const u = o.universe;
  const tabs = [["gainers", "Top gainers"], ["losers", "Top losers"], ["active", "Most active"]] as const;
  return (
    <div className="markets">
      <div className="mk-tiles" role="group" aria-label="Indexes and funds">
        {o.indices.map((t: any) => (
          <button key={t.symbol} className="mk-tile" onClick={() => onSelect(t.symbol)} aria-label={`${t.symbol} ${pct(t.change_pct)}`}>
            <span className="mk-tile-top"><b>{t.symbol}</b><span className={tone(t.change_pct)}>{pct(t.change_pct)}</span></span>
            <span className="muted mk-tile-name">{t.name}</span>
            <span className="mk-tile-bottom"><span>{money(t.last)}</span><Spark points={t.sparkline} width={70} height={20} label={`${t.symbol} intraday line`} /></span>
          </button>
        ))}
        {o.indices.length === 0 && <p className="muted">No index or fund prices yet.</p>}
      </div>

      <section className="mk-breadth" aria-label="Market breadth">
        <h3>Breadth</h3>
        <div className="mk-bar" role="img" aria-label={`${b.advancers} rising, ${b.decliners} falling, ${b.unchanged} unchanged`}>
          <i className="up-bg" style={{ width: `${adv}%` }} /><i className="flat-bg" style={{ width: `${100 - adv - dec}%` }} /><i className="down-bg" style={{ width: `${dec}%` }} />
        </div>
        <div className="mk-stats">
          <div><span>Rising</span><b className="up">{b.advancers}</b></div>
          <div><span>Falling</span><b className="down">{b.decliners}</b></div>
          <div><span>Unchanged</span><b>{b.unchanged}</b></div>
          <div><span>Up / down volume</span><b>{compact(b.up_volume)} / {compact(b.down_volume)}</b></div>
          <div><span>52-week highs / lows</span><b>{b.new_highs} / {b.new_lows}</b></div>
          <div><span>Above 50-day average</span><b>{b.with_history ? `${Math.round((b.above_sma50 / b.with_history) * 100)}%` : "–"}</b></div>
        </div>
      </section>

      <section className="mk-heat" aria-label="Sectors">
        <div className="mk-heat-head">
          <h3>Sectors</h3>
          <div className="seg" role="group" aria-label="Sector display">
            <button className={map === "map" ? "on" : ""} aria-pressed={map === "map"} onClick={() => setMap("map")}>Heatmap</button>
            <button className={map === "list" ? "on" : ""} aria-pressed={map === "list"} onClick={() => setMap("list")}>Ranked list</button>
          </div>
        </div>
        <p className="muted mk-note">{o.sector_note}</p>
        {o.sectors.length === 0 ? <p className="muted pad">No sector data yet.</p>
          : map === "map" ? <Heatmap sectors={o.sectors} onSelect={onSelect} /> : <SectorList sectors={o.sectors} onSelect={onSelect} />}
      </section>

      {small ? (
        <section className="mk-movers-one">
          <div className="seg" role="group" aria-label="Movers">
            {tabs.map(([k, label]) => <button key={k} className={movers === k ? "on" : ""} aria-pressed={movers === k} onClick={() => setMovers(k)}>{label}</button>)}
          </div>
          <MoverTable title={tabs.find((t) => t[0] === movers)![1]} rows={o[movers]} onSelect={onSelect} volume={movers === "active"} />
        </section>
      ) : (
        <div className="mk-movers-grid">
          {tabs.map(([k, label]) => <MoverTable key={k} title={label} rows={o[k]} onSelect={onSelect} volume={k === "active"} />)}
        </div>
      )}
      <p className="muted mk-foot">
        {u.label}{u.remote ? ` · prices for ${u.with_quotes.toLocaleString()} of ${u.total.toLocaleString()} fetched${u.fetched_to ? ` by ${dateTime(u.fetched_to)}` : ""}` : ""}
        {o.mover_floor ? ` · movers priced at least $${o.mover_floor.min_price} with at least ${compact(o.mover_floor.min_volume)} shares traded` : ""}
        {o.simulated ? " · simulated prices for fictional companies" : ""}
      </p>
    </div>
  );
}
