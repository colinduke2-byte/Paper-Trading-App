import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RangeBar, Spark } from "./Spark";
import { SymbolSearch } from "./SymbolSearch";
import { api, compact, money, pct, qty, signed, store, tone } from "./util";
import "./discovery.css";

// Several named watchlists, each with its own columns, density and sort, saved on the server
// (/api/watchlist-sets). Live prices come from the app's WebSocket quotes; the other columns are
// fetched every 15 s. Asking for a list's rows also tells a live feed which list is on screen, so
// that list is streamed first (the free Alpaca plan streams 30 symbols; the rest are polled).

export const WATCHLISTS_CHANGED = "pd:watchlists-changed";
export const watchlistsChanged = () => window.dispatchEvent(new Event(WATCHLISTS_CHANGED));

type Layout = { columns: string[]; density: "compact" | "comfortable"; sort: { key: string; desc: boolean } | null };
type WList = { name: string; symbols: string[]; layout: Layout };
type Sets = { lists: WList[]; columns: { key: string; label: string }[]; limits: { lists: number; symbols: number } };
type Panel = null | "lists" | "columns" | "new" | "rename" | "duplicate" | "delete";

const SHORT: Record<string, string> = {
  last: "Last", change: "Chg", change_pct: "Chg %", bid: "Bid", ask: "Ask", volume: "Volume", day_range: "Day range",
  range_52w: "52-wk", market_cap: "Mkt cap", pe: "P/E", dividend_yield: "Yield", position_qty: "Position",
  unrealized_pnl: "Unrl. gain", sparkline: "Today",
};
const enc = encodeURIComponent;

export function Watchlists({ quotes, positions, selected, accountId, onSelect, onVisible, notify }: {
  quotes: Record<string, any>; positions: any[]; selected: string; accountId: string;
  onSelect: (s: string) => void; onVisible: (symbols: string[]) => void; notify: (m: string, bad?: boolean) => void;
}) {
  const [sets, setSets] = useState<Sets | null>(null);
  const [active, setActive] = useState<string>(() => store("pd.watchlist") || "");
  const [rows, setRows] = useState<Record<string, any>>({});
  const [meta, setMeta] = useState<{ note?: string | null; sources: string[] }>({ sources: [] });
  const [panel, setPanel] = useState<Panel>(null);
  const [reorder, setReorder] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const dragFrom = useRef<string | null>(null);

  const fail = (what: string) => (e: any) => notify(`Could not ${what}: ${e.message}`, true);
  const load = useCallback(() => api("/watchlist-sets").then(setSets).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    window.addEventListener(WATCHLISTS_CHANGED, load);
    return () => window.removeEventListener(WATCHLISTS_CHANGED, load);
  }, [load]);

  const list = sets?.lists.find((l) => l.name === active) || sets?.lists[0];
  useEffect(() => { if (list && list.name !== active) setActive(list.name); }, [list?.name]);
  useEffect(() => { if (active) store("pd.watchlist", active); }, [active]);
  const symbols = list?.symbols || [];
  const layout = list?.layout || { columns: ["last", "change", "change_pct"], density: "comfortable", sort: null };
  useEffect(() => { onVisible(symbols); }, [symbols.join(",")]);

  // Columns that need more than the live quote are fetched every 15 s (this also declares the list as shown).
  const colKey = layout.columns.join(",");
  useEffect(() => {
    if (!list) return;
    let alive = true;
    const get = () => api(`/watchlist-sets/${enc(list.name)}/rows?columns=${enc(colKey)}&account=${enc(accountId || "")}`)
      .then((j) => {
        if (!alive) return;
        setRows(Object.fromEntries(j.rows.map((r: any) => [r.symbol, r])));
        setMeta({ note: j.note, sources: [...new Set<string>(j.rows.map((r: any) => r.quote_source).filter(Boolean))] });
      }).catch(() => {});
    get();
    const t = setInterval(get, 15000);
    return () => { alive = false; clearInterval(t); };
  }, [list?.name, colKey, accountId, symbols.join(",")]);

  const posBy = useMemo(() => Object.fromEntries(positions.filter((p) => !p.underlying).map((p) => [p.symbol, p])), [positions]);
  const value = (s: string, key: string): any => {
    const q = quotes[s], r = rows[s] || {};
    switch (key) {
      case "symbol": return s;
      case "name": return r.name;
      case "last": case "change": case "change_pct": case "bid": case "ask": case "volume": return q?.[key] ?? r[key];
      case "day_range": { const lo = q?.low ?? r.low, hi = q?.high ?? r.high, last = q?.last ?? r.last; return hi > lo ? (last - lo) / (hi - lo) : null; }
      case "range_52w": return r.range_52w_pos;
      case "position_qty": return posBy[s]?.qty ?? r.position_qty;
      case "unrealized_pnl": return posBy[s]?.unrealized_pnl ?? r.unrealized_pnl;
      default: return r[key];
    }
  };
  const shown = useMemo(() => {
    const sort = layout.sort;
    if (!sort) return symbols;
    const withVal = symbols.map((s) => [s, value(s, sort.key)] as const);
    const present = withVal.filter(([, v]) => v != null), missing = withVal.filter(([, v]) => v == null);
    present.sort((a, b) => (typeof a[1] === "string" ? String(a[1]).localeCompare(String(b[1])) : (a[1] as number) - (b[1] as number)) * (sort.desc ? -1 : 1));
    return [...present, ...missing].map(([s]) => s);
  }, [symbols.join(","), layout.sort?.key, layout.sort?.desc, quotes, rows, posBy]);

  // ---- server changes -----------------------------------------------------------------------------
  const patch = (body: any, what: string) => {
    if (!list) return Promise.resolve();
    const name = list.name;
    // A rename takes effect when the server has made it: switching to the new name sooner sends the rows request
    // for a list the server does not know yet (HTTP 404).
    if (body.name) return api(`/watchlist-sets/${enc(name)}`, { method: "PATCH", body })
      .then((j) => { setSets(j); setActive(body.name); }).catch((e) => { fail(what)(e); load(); });
    setSets((cur) => cur && { ...cur, lists: cur.lists.map((l) => l.name !== name ? l : {
      ...l, symbols: body.symbols ?? l.symbols,
      layout: { ...l.layout, ...Object.fromEntries(Object.entries(body).filter(([k]) => ["columns", "density", "sort"].includes(k))) } as Layout,
    }) });
    return api(`/watchlist-sets/${enc(name)}`, { method: "PATCH", body }).then(setSets).catch((e) => { fail(what)(e); load(); });
  };
  const add = (s: string) => {
    if (!list) return;
    onSelect(s);
    if (list.symbols.includes(s)) return;
    api(`/watchlist-sets/${enc(list.name)}/symbols`, { method: "POST", body: { symbols: [s] } })
      .then((r) => { if (r.full.length) notify(`${list.name} is full (${sets?.limits.symbols} symbols); ${s} was not added.`, true); load(); })
      .catch(fail(`add ${s}`));
  };
  const remove = (s: string) => patch({ symbols: symbols.filter((x) => x !== s) }, `remove ${s}`);
  const move = (s: string, to: number) => {
    const next = symbols.filter((x) => x !== s);
    next.splice(Math.max(0, Math.min(to, next.length)), 0, s);
    patch({ symbols: next }, "reorder the list");
  };
  const setSort = (key: string) => {
    const cur = layout.sort;
    const next = !cur || cur.key !== key ? { key, desc: key !== "symbol" } : cur.desc === (key !== "symbol") ? { key, desc: !cur.desc } : null;
    patch({ sort: next }, "sort the list");
  };
  const submitName = async () => {
    const name = draft.trim();
    if (!name) return;
    try {
      if (panel === "new") { setSets(await api("/watchlist-sets", { method: "POST", body: { name } })); setActive(name); notify(`Watchlist "${name}" created`); }
      else if (panel === "rename" && list) { await patch({ name }, "rename the list"); }
      else if (panel === "duplicate" && list) { setSets(await api(`/watchlist-sets/${enc(list.name)}/duplicate`, { method: "POST", body: { name } })); setActive(name); }
      setPanel(null); setDraft("");
    } catch (e: any) { notify(`Could not save the watchlist: ${e.message}`, true); }
  };
  const del = async () => {
    if (!list) return;
    try { const j = await api(`/watchlist-sets/${enc(list.name)}`, { method: "DELETE" }); setSets(j); setActive(j.lists[0]?.name || ""); setPanel(null); notify(`Watchlist "${list.name}" deleted`); }
    catch (e: any) { notify(`Could not delete the watchlist: ${e.message}`, true); }
  };
  const moveList = (dir: number) => {
    if (!sets || !list) return;
    const names = sets.lists.map((l) => l.name);
    const i = names.indexOf(list.name), j = i + dir;
    if (j < 0 || j >= names.length) return;
    [names[i], names[j]] = [names[j], names[i]];
    api("/watchlist-order", { method: "PUT", body: names }).then(setSets).catch(fail("reorder the watchlists"));
  };
  const toggleColumn = (key: string, on: boolean) => {
    const all = (sets?.columns || []).map((c) => c.key);
    const next = all.filter((k) => (k === key ? on : layout.columns.includes(k)));
    patch({ columns: next }, "change the columns");
  };

  // ---- cells ---------------------------------------------------------------------------------------
  const cell = (s: string, key: string) => {
    const q = quotes[s], r = rows[s] || {};
    const v = (k: string) => q?.[k] ?? r[k];
    switch (key) {
      case "last": return <td key={key} className="num">{money(v("last"))}</td>;
      case "change": return <td key={key} className={"num " + tone(v("change"))}>{signed(v("change"))}</td>;
      case "change_pct": return <td key={key} className={"num " + tone(v("change_pct"))}>{pct(v("change_pct"))}</td>;
      case "bid": case "ask": return <td key={key} className="num">{money(v(key))}</td>;
      case "volume": return <td key={key} className="num">{compact(v("volume"))}</td>;
      case "day_range": return <td key={key} className="num"><RangeBar pos={value(s, "day_range") == null ? null : value(s, "day_range") * 100} low={v("low")} high={v("high")} label={`${s} day range`} /></td>;
      case "range_52w": return <td key={key} className="num"><RangeBar pos={r.range_52w_pos} low={r.low_52w} high={r.high_52w} label={`${s} 52-week range`} /></td>;
      case "market_cap": return <td key={key} className="num">{compact(r.market_cap)}</td>;
      case "pe": return <td key={key} className="num">{r.pe == null ? "–" : r.pe.toFixed(1)}</td>;
      case "dividend_yield": return <td key={key} className="num">{r.dividend_yield == null ? "–" : r.dividend_yield.toFixed(2) + "%"}</td>;
      case "position_qty": return <td key={key} className={"num " + tone(value(s, key))}>{value(s, key) == null ? "" : qty(value(s, key))}</td>;
      case "unrealized_pnl": return <td key={key} className={"num " + tone(value(s, key))}>{value(s, key) == null ? "" : signed(value(s, key))}</td>;
      case "sparkline": return <td key={key} className="num"><Spark points={r.sparkline} label={`${s} intraday line`} /></td>;
    }
    return <td key={key} />;
  };
  const head = (key: string, label: string, num = true) => {
    const s = layout.sort?.key === key ? layout.sort : null;
    return (
      <th key={key} className={num ? "num" : ""} aria-sort={s ? (s.desc ? "descending" : "ascending") : "none"}>
        <button className="th-sort" title={`Sort by ${label.toLowerCase()}`} onClick={() => setSort(key)}>
          {label}{s ? (s.desc ? " ▼" : " ▲") : ""}
        </button>
      </th>
    );
  };

  if (error && !sets) return <section className="panel watch" aria-label="Watchlist"><div className="notice bad">Could not load watchlists: {error}</div></section>;
  const nameForm = (title: string, button: string) => (
    <form className="wl-form" onSubmit={(e) => { e.preventDefault(); submitName(); }}>
      <label>{title}<input value={draft} maxLength={30} autoFocus onChange={(e) => setDraft(e.target.value)} /></label>
      <div className="row"><button type="button" className="ghost sm" onClick={() => setPanel(null)}>Cancel</button>
        <button type="submit" className="primary sm" disabled={!draft.trim()}>{button}</button></div>
    </form>
  );
  const idx = sets && list ? sets.lists.findIndex((l) => l.name === list.name) : -1;
  const canDrag = reorder && !layout.sort;

  return (
    <section className={`panel watch${layout.columns.length > 5 ? " wl-wider" : layout.columns.length > 3 ? " wl-wide" : ""}`} aria-label="Watchlist">
      <div className="wl-head">
        <select aria-label="Choose watchlist" value={list?.name || ""} onChange={(e) => { setActive(e.target.value); setPanel(null); }}>
          {(sets?.lists || []).map((l) => <option key={l.name} value={l.name}>{l.name} ({l.symbols.length})</option>)}
        </select>
        <button className={"ghost sm" + (panel === "lists" ? " on" : "")} aria-expanded={panel === "lists"} onClick={() => setPanel(panel === "lists" ? null : "lists")}>Lists</button>
        <button className={"ghost sm" + (panel === "columns" ? " on" : "")} aria-expanded={panel === "columns"} onClick={() => setPanel(panel === "columns" ? null : "columns")}>Columns</button>
        <button className={"ghost sm" + (reorder ? " on" : "")} aria-pressed={reorder} onClick={() => setReorder((x) => !x)}>{reorder ? "Done" : "Reorder"}</button>
      </div>
      {panel === "lists" && (
        <div className="wl-panel" role="group" aria-label="Watchlist actions">
          <button className="ghost sm" onClick={() => { setDraft(""); setPanel("new"); }}>New list</button>
          <button className="ghost sm" onClick={() => { setDraft(list?.name || ""); setPanel("rename"); }}>Rename</button>
          <button className="ghost sm" onClick={() => { setDraft(`${list?.name || ""} copy`.slice(0, 30)); setPanel("duplicate"); }}>Duplicate</button>
          <button className="ghost sm" disabled={idx <= 0} onClick={() => moveList(-1)}>Move up</button>
          <button className="ghost sm" disabled={!sets || idx >= sets.lists.length - 1} onClick={() => moveList(1)}>Move down</button>
          <button className="ghost sm danger" disabled={(sets?.lists.length || 0) <= 1} onClick={() => setPanel("delete")}>Delete</button>
        </div>
      )}
      {panel === "new" && nameForm("New watchlist name", "Create")}
      {panel === "rename" && nameForm("New name", "Rename")}
      {panel === "duplicate" && nameForm("Name of the copy", "Duplicate")}
      {panel === "delete" && (
        <div className="wl-form" role="alertdialog" aria-label="Delete watchlist">
          <p>Delete the watchlist "{list?.name}"? Its {symbols.length} symbols are removed from it; positions and orders are not affected.</p>
          <div className="row"><button className="ghost sm" onClick={() => setPanel(null)}>Cancel</button>
            <button className="primary sm danger" onClick={del}>Delete list</button></div>
        </div>
      )}
      {panel === "columns" && (
        <div className="wl-panel wl-cols" role="group" aria-label="Columns">
          {(sets?.columns || []).map((c) => (
            <label key={c.key} className="check"><input type="checkbox" checked={layout.columns.includes(c.key)}
              onChange={(e) => toggleColumn(c.key, e.target.checked)} />{c.label}</label>
          ))}
          <div className="seg" role="group" aria-label="Row density">
            {(["compact", "comfortable"] as const).map((d) => (
              <button key={d} className={layout.density === d ? "on" : ""} aria-pressed={layout.density === d}
                onClick={() => patch({ density: d }, "change the density")}>{d === "compact" ? "Compact" : "Comfortable"}</button>
            ))}
          </div>
        </div>
      )}
      <SymbolSearch onPick={add} assetFilter />
      {reorder && layout.sort && (
        <p className="muted wl-hint">Sorted by {SHORT[layout.sort.key] || layout.sort.key}. <button className="link" onClick={() => patch({ sort: null }, "clear the sort")}>Clear the sort</button> to reorder by hand.</p>
      )}
      {reorder && !layout.sort && <p className="muted wl-hint">Drag rows or use the arrows to change the order.</p>}
      <div className="scroll">
        <table className={`tight wl-table wl-${layout.density}`}>
          <thead><tr>
            {reorder && <th />}
            {head("symbol", "Symbol", false)}
            {layout.columns.map((k) => head(k, SHORT[k] || k))}
            <th />
          </tr></thead>
          <tbody>
            {shown.map((s, i) => (
              <tr key={s} className={(s === selected ? "sel click" : "click")} title={rows[s]?.name}
                draggable={canDrag} onClick={() => onSelect(s)}
                onDragStart={(e) => { dragFrom.current = s; e.dataTransfer.effectAllowed = "move"; }}
                onDragOver={(e) => { if (canDrag && dragFrom.current) e.preventDefault(); }}
                onDrop={(e) => { e.preventDefault(); const from = dragFrom.current; dragFrom.current = null; if (from && from !== s) move(from, symbols.indexOf(s)); }}>
                {reorder && (
                  <td className="wl-move" onClick={(e) => e.stopPropagation()}>
                    <button className="x" aria-label={`Move ${s} up`} disabled={!!layout.sort || i === 0} onClick={() => move(s, i - 1)}>↑</button>
                    <button className="x" aria-label={`Move ${s} down`} disabled={!!layout.sort || i === shown.length - 1} onClick={() => move(s, i + 1)}>↓</button>
                  </td>
                )}
                <td><b>{s}</b></td>
                {layout.columns.map((k) => <Fragment key={k}>{cell(s, k)}</Fragment>)}
                <td className="num"><button className="x" title={`Remove ${s}`} aria-label={`Remove ${s}`}
                  onClick={(e) => { e.stopPropagation(); remove(s); }}>×</button></td>
              </tr>
            ))}
            {sets && symbols.length === 0 && <tr><td colSpan={layout.columns.length + 3} className="muted">This list is empty. Search above to add symbols.</td></tr>}
          </tbody>
        </table>
      </div>
      {(meta.sources.length > 0 || meta.note) && (
        <p className="muted wl-foot">Prices: {meta.sources.join(", ") || "–"}{meta.note ? ` · ${meta.note}` : ""}</p>
      )}
    </section>
  );
}
