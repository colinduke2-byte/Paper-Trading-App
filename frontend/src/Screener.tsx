import { useEffect, useMemo, useState } from "react";
import { watchlistsChanged } from "./Watchlists";
import { api, compact, dateTime, money, pct } from "./util";
import "./discovery.css";

// Filter builder over /api/screener/run. Builder rows are friendlier than raw fields (for example
// "price above the 50-day average"); each one turns into a {field, min, max} filter on the server.

type Field = { key: string; label: string; unit?: string; kind: string; history?: boolean; available: boolean; choices?: string[] };
type Row =
  | { id: number; kind: "range"; field: string; min: string; max: string }
  | { id: number; kind: "ma"; side: "above" | "below"; period: "20" | "50" | "200" }
  | { id: number; kind: "gap"; dir: "up" | "down"; pct: string }
  | { id: number; kind: "near"; which: "high" | "low"; pct: string }
  | { id: number; kind: "type"; value: "stock" | "etf" }
  | { id: number; kind: "sector"; values: string[] }
  | { id: number; kind: "options"; value: boolean };

const KINDS: { kind: string; label: string }[] = [
  { kind: "range:last", label: "Price" }, { kind: "range:change_pct", label: "Change %" },
  { kind: "range:volume", label: "Volume" }, { kind: "range:rel_volume", label: "Relative volume" },
  { kind: "range:market_cap", label: "Market cap" }, { kind: "range:pe", label: "P/E" },
  { kind: "range:dividend_yield", label: "Dividend yield %" }, { kind: "near:high", label: "Near 52-week high" },
  { kind: "near:low", label: "Near 52-week low" }, { kind: "gap", label: "Gap up or down" },
  { kind: "ma", label: "Price vs moving average" }, { kind: "range:rsi14", label: "RSI (14 days)" },
  { kind: "type", label: "Asset type" }, { kind: "sector", label: "Sector" }, { kind: "options", label: "Has options" },
  { kind: "range:dollar_volume", label: "Dollar volume" }, { kind: "range:volatility", label: "Volatility" },
];
const COLS: Record<string, string> = {
  symbol: "Symbol", name: "Name", sector: "Sector", last: "Last", change_pct: "Chg %", gap_pct: "Gap %", volume: "Volume", rel_volume: "Rel vol",
  market_cap: "Mkt cap", pe: "P/E", dividend_yield: "Yield", pct_from_high_52w: "Below high %", rsi14: "RSI",
  pct_vs_sma50: "vs 50-day %", pct_vs_sma200: "vs 200-day %", dollar_volume: "$ volume", volatility: "Volatility",
};
const DEFAULT_COLS = ["name", "last", "change_pct", "volume", "market_cap", "rsi14"];
let nextId = 1;

function toFilters(rows: Row[]): any[] {
  const n = (s: string) => (s.trim() === "" ? null : Number(s));
  const out: any[] = [];
  for (const r of rows) {
    if (r.kind === "range") { if (r.min.trim() || r.max.trim()) out.push({ field: r.field, min: n(r.min), max: n(r.max) }); }
    else if (r.kind === "ma") out.push(r.side === "above" ? { field: `pct_vs_sma${r.period}`, min: 0 } : { field: `pct_vs_sma${r.period}`, max: 0 });
    else if (r.kind === "gap") { const v = Math.abs(Number(r.pct || 0)); out.push(r.dir === "up" ? { field: "gap_pct", min: v } : { field: "gap_pct", max: -v }); }
    else if (r.kind === "near") out.push({ field: r.which === "high" ? "pct_from_high_52w" : "pct_from_low_52w", max: Number(r.pct || 0) });
    else if (r.kind === "type") out.push({ field: "type", in: [r.value] });
    else if (r.kind === "sector") { if (r.values.length) out.push({ field: "sector", in: r.values }); }
    else if (r.kind === "options") out.push({ field: "has_options", equals: r.value });
  }
  return out;
}

function fromFilters(filters: any[]): Row[] {
  const s = (v: any) => (v == null ? "" : String(v));
  return filters.map((f) => {
    const id = nextId++;
    const ma = /^pct_vs_sma(20|50|200)$/.exec(f.field);
    if (ma && (f.min === 0 && f.max == null || f.max === 0 && f.min == null)) return { id, kind: "ma", side: f.min === 0 ? "above" : "below", period: ma[1] as any };
    if (f.field === "gap_pct" && (f.min == null) !== (f.max == null)) return { id, kind: "gap", dir: f.min != null ? "up" : "down", pct: s(Math.abs(f.min ?? f.max)) };
    if ((f.field === "pct_from_high_52w" || f.field === "pct_from_low_52w") && f.min == null) return { id, kind: "near", which: f.field.includes("high") ? "high" : "low", pct: s(f.max) };
    if (f.field === "type") return { id, kind: "type", value: f.in[0] };
    if (f.field === "sector") return { id, kind: "sector", values: f.in };
    if (f.field === "has_options") return { id, kind: "options", value: !!f.equals };
    return { id, kind: "range", field: f.field, min: s(f.min), max: s(f.max) };
  });
}

function newRow(kind: string): Row {
  const id = nextId++;
  if (kind.startsWith("range:")) return { id, kind: "range", field: kind.slice(6), min: "", max: "" };
  if (kind === "near:high" || kind === "near:low") return { id, kind: "near", which: kind === "near:high" ? "high" : "low", pct: "5" };
  if (kind === "gap") return { id, kind: "gap", dir: "up", pct: "2" };
  if (kind === "ma") return { id, kind: "ma", side: "above", period: "50" };
  if (kind === "type") return { id, kind: "type", value: "stock" };
  if (kind === "sector") return { id, kind: "sector", values: [] };
  return { id, kind: "options", value: true };
}

const fmt = (key: string, v: any) => {
  if (v == null) return "–";
  if (key === "name" || key === "sector") return String(v);
  if (key === "last") return money(v);
  if (key === "change_pct" || key === "gap_pct") return pct(v);
  if (key === "volume" || key === "market_cap" || key === "dollar_volume") return compact(v);
  if (key === "rel_volume") return v.toFixed(2) + "×";
  if (key === "dividend_yield" || key === "pct_from_high_52w" || key === "pct_vs_sma50" || key === "pct_vs_sma200") return v.toFixed(2) + "%";
  if (key === "volatility") return (v * 100).toFixed(0) + "%";
  return typeof v === "number" ? v.toFixed(1) : String(v);
};

export function Screener({ onSelect, notify }: { onSelect: (s: string) => void; notify: (m: string, bad?: boolean) => void }) {
  const [meta, setMeta] = useState<any>(null);
  const [saved, setSaved] = useState<any[]>([]);
  const [rows, setRows] = useState<Row[]>(() => [newRow("range:last")]);
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "market_cap", desc: true });
  const [cols, setCols] = useState<string[]>(DEFAULT_COLS);
  const [res, setRes] = useState<any>(null);
  const [error, setError] = useState("");
  const [screenId, setScreenId] = useState("");
  const [saveName, setSaveName] = useState("");
  const [showCols, setShowCols] = useState(false);
  const [lists, setLists] = useState<string[]>([]);
  const [target, setTarget] = useState("");
  const [n, setN] = useState(0);

  const loadSaved = () => api("/screens").then((j) => setSaved(j.saved)).catch(() => {});
  useEffect(() => { api("/screener/fields").then(setMeta).catch((e) => setError(e.message)); loadSaved(); }, []);
  useEffect(() => { api("/watchlist-sets").then((j) => setLists(j.lists.map((l: any) => l.name))).catch(() => {}); }, []);

  const filters = useMemo(() => toFilters(rows), [rows]);
  const key = JSON.stringify([filters, sort]);
  useEffect(() => {
    let alive = true;
    const run = () => api("/screener/run", { method: "POST", body: { filters, sort: sort.key, desc: sort.desc, limit: 100 } })
      .then((j) => { if (alive) { setRes(j); setError(""); } })
      .catch((e) => alive && setError(e.message));
    const t = setTimeout(run, 250);
    const every = setInterval(run, res?.universe?.remote ? 15000 : 5000);
    return () => { alive = false; clearTimeout(t); clearInterval(every); };
  }, [key, n, !!res?.universe?.remote]);

  const fields: Record<string, Field> = Object.fromEntries((meta?.fields || []).map((f: Field) => [f.key, f]));
  const presets: any[] = meta?.presets || [];
  const all = [...presets, ...saved];
  const pickScreen = (id: string) => {
    setScreenId(id);
    const s = all.find((x) => x.id === id);
    if (!s) return;
    setRows(fromFilters(s.filters));
    if (s.sort) setSort({ key: s.sort, desc: s.desc !== false });
    if (s.columns?.length) setCols(s.columns);
    setSaveName(s.preset ? "" : s.name);
  };
  const current = saved.find((s) => s.id === screenId);
  const body = (name: string) => ({ name, filters, sort: sort.key, desc: sort.desc, columns: cols });
  const save = async () => {
    try {
      const s = current && current.name === saveName.trim()
        ? await api(`/screens/${current.id}`, { method: "PUT", body: body(saveName.trim()) })
        : await api("/screens", { method: "POST", body: body(saveName.trim()) });
      await loadSaved(); setScreenId(s.id); notify(`Screen "${s.name}" saved`);
    } catch (e: any) { notify(`Could not save the screen: ${e.message}`, true); }
  };
  const remove = async () => {
    if (!current) return;
    try { await api(`/screens/${current.id}`, { method: "DELETE" }); setScreenId(""); setSaveName(""); loadSaved(); notify(`Screen "${current.name}" deleted`); }
    catch (e: any) { notify(`Could not delete the screen: ${e.message}`, true); }
  };
  const addAll = async () => {
    const name = target || lists[0];
    if (!name || !res?.rows?.length) return;
    try {
      const r = await api(`/watchlist-sets/${encodeURIComponent(name)}/symbols`, { method: "POST", body: { symbols: res.rows.map((x: any) => x.symbol) } });
      const parts = [`Added ${r.added.length} to ${name}`];
      if (r.already.length) parts.push(`${r.already.length} already there`);
      if (r.full.length) parts.push(`${r.full.length} did not fit (a list holds 100 symbols)`);
      notify(parts.join("; "));
      watchlistsChanged();
    } catch (e: any) { notify(`Could not add to the watchlist: ${e.message}`, true); }
  };
  const update = (id: number, patch: any) => { setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r))); setScreenId((s) => (saved.some((x) => x.id === s) ? s : "")); };
  const u = res?.universe || meta?.universe;
  const unavailable = (k: string) => fields[k] && !fields[k].available;

  const control = (r: Row) => {
    const num = (v: string, onChange: (v: string) => void, label: string) =>
      <input className="sc-num" inputMode="decimal" value={v} aria-label={label} placeholder={label.split(" ").pop()} onChange={(e) => onChange(e.target.value)} />;
    switch (r.kind) {
      case "range": {
        const f = fields[r.field];
        return (<>
          <select aria-label="Filter field" value={r.field} onChange={(e) => update(r.id, { field: e.target.value })}>
            {(meta?.fields || []).filter((x: Field) => x.kind === "number").map((x: Field) => <option key={x.key} value={x.key}>{x.label}</option>)}
          </select>
          {num(r.min, (v) => update(r.id, { min: v }), `${f?.label || r.field} minimum`)}
          {num(r.max, (v) => update(r.id, { max: v }), `${f?.label || r.field} maximum`)}
          {unavailable(r.field) && <span className="muted">not provided by this feed</span>}
        </>);
      }
      case "ma": return (<>
        <span>Price</span>
        <select aria-label="Above or below" value={r.side} onChange={(e) => update(r.id, { side: e.target.value })}><option value="above">above</option><option value="below">below</option></select>
        <select aria-label="Moving average" value={r.period} onChange={(e) => update(r.id, { period: e.target.value })}><option value="20">20-day average</option><option value="50">50-day average</option><option value="200">200-day average</option></select>
      </>);
      case "gap": return (<>
        <select aria-label="Gap direction" value={r.dir} onChange={(e) => update(r.id, { dir: e.target.value })}><option value="up">Gap up at least</option><option value="down">Gap down at least</option></select>
        {num(r.pct, (v) => update(r.id, { pct: v }), "Gap percent")}<span>%</span>
      </>);
      case "near": return (<>
        <span>Within</span>{num(r.pct, (v) => update(r.id, { pct: v }), `Percent from 52-week ${r.which}`)}<span>% of the 52-week {r.which}</span>
      </>);
      case "type": return (<>
        <span>Asset type</span>
        <select aria-label="Asset type" value={r.value} onChange={(e) => update(r.id, { value: e.target.value })}><option value="stock">Stocks</option><option value="etf">Funds (ETFs)</option></select>
      </>);
      case "sector": return (<>
        <span>Sector is</span>
        <span className="sc-sectors">
          {(fields.sector?.choices || []).map((s) => (
            <label key={s} className="check"><input type="checkbox" checked={r.values.includes(s)}
              onChange={(e) => update(r.id, { values: e.target.checked ? [...r.values, s] : r.values.filter((x) => x !== s) })} />{s}</label>
          ))}
          {unavailable("sector") && <span className="muted">Sector data is not provided by this feed.</span>}
        </span>
      </>);
      case "options": return (<>
        <span>Has options</span>
        <select aria-label="Has options" value={String(r.value)} onChange={(e) => update(r.id, { value: e.target.value === "true" })}><option value="true">Yes</option><option value="false">No</option></select>
        {unavailable("has_options") && <span className="muted">not known for this feed</span>}
      </>);
    }
  };
  const th = (k: string) => {
    const on = sort.key === k;
    return (
      <th key={k} className={k === "name" || k === "sector" ? "" : "num"} aria-sort={on ? (sort.desc ? "descending" : "ascending") : "none"}>
        <button className="th-sort" onClick={() => setSort(on ? { key: k, desc: !sort.desc } : { key: k, desc: !["symbol", "name", "sector"].includes(k) })}>
          {COLS[k] || k}{on ? (sort.desc ? " ▼" : " ▲") : ""}
        </button>
      </th>
    );
  };

  return (
    <div className="screener">
      {u && (
        <div className="notice sc-universe" role="note">
          <b>Universe:</b> {u.label}
          {u.remote ? <> · prices for {u.with_quotes.toLocaleString()} of {u.total.toLocaleString()} fetched
            {u.fetched_from ? ` between ${dateTime(u.fetched_from)} and ${dateTime(u.fetched_to)}` : " (starting)"}
            {u.complete ? "" : "; the scan continues while this tab is open"}</> : " · screened live"}
          {u.error && <span className="down"> · {u.error}</span>}
          <details><summary>How this is screened</summary>{u.note}</details>
        </div>
      )}
      <div className="toolbar sc-saved">
        <select aria-label="Saved screen" value={screenId} onChange={(e) => pickScreen(e.target.value)}>
          <option value="">Choose a saved screen…</option>
          <optgroup label="Built in">{presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</optgroup>
          {saved.length > 0 && <optgroup label="Saved">{saved.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</optgroup>}
        </select>
        <input value={saveName} aria-label="Screen name" placeholder="Screen name" maxLength={30} onChange={(e) => setSaveName(e.target.value)} />
        <button className="ghost sm" disabled={!saveName.trim()} onClick={save}>{current && current.name === saveName.trim() ? "Update screen" : "Save screen"}</button>
        {current && <button className="ghost sm" onClick={remove}>Delete screen</button>}
      </div>
      <div className="sc-filters" role="group" aria-label="Filters">
        {rows.map((r) => (
          <div key={r.id} className="sc-row">
            {control(r)}
            <button className="x" aria-label="Remove filter" title="Remove filter" onClick={() => setRows((rs) => rs.filter((x) => x.id !== r.id))}>×</button>
          </div>
        ))}
        <div className="sc-row">
          <select aria-label="Add filter" value="" onChange={(e) => { if (e.target.value) setRows((rs) => [...rs, newRow(e.target.value)]); }}>
            <option value="">Add a filter…</option>
            {KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
          </select>
          {rows.length > 0 && <button className="ghost sm" onClick={() => setRows([])}>Clear filters</button>}
          <button className="ghost sm" aria-expanded={showCols} onClick={() => setShowCols((x) => !x)}>Result columns</button>
        </div>
        {showCols && (
          <div className="wl-panel wl-cols" role="group" aria-label="Result columns">
            {Object.entries(COLS).filter(([k]) => k !== "symbol").map(([k, label]) => (
              <label key={k} className="check"><input type="checkbox" checked={cols.includes(k)}
                onChange={(e) => setCols(Object.keys(COLS).filter((x) => x !== "symbol").filter((x) => (x === k ? e.target.checked : cols.includes(x))))} />{label}</label>
            ))}
          </div>
        )}
      </div>
      {error && <div className="notice bad" role="alert">The screen could not run: {error}</div>}
      {res && (
        <div className="toolbar sc-summary">
          <span>{res.matched === 0 ? "No symbols match these filters." : `${res.matched.toLocaleString()} match${res.matched === 1 ? "" : "es"}${res.matched > res.rows.length ? `, showing the first ${res.rows.length}` : ""}`} · {res.screened.toLocaleString()} screened</span>
          {res.history_pending > 0 && <span className="muted">{res.history_pending.toLocaleString()} more need daily history, fetched a few at a time</span>}
          <span className="sc-add">
            <select aria-label="Watchlist to add to" value={target || lists[0] || ""} onChange={(e) => setTarget(e.target.value)}>
              {lists.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
            <button className="ghost sm" disabled={!res.rows.length || !lists.length} onClick={addAll}>Add all to watchlist</button>
            <button className="ghost sm" onClick={() => setN((x) => x + 1)}>Run again</button>
          </span>
        </div>
      )}
      {res && res.rows.length > 0 && (
        <div className="sc-results">
          <table className="sc-table">
            <thead><tr>{th("symbol")}{cols.map(th)}</tr></thead>
            <tbody>
              {res.rows.map((r: any) => (
                <tr key={r.symbol} className="click" onClick={() => onSelect(r.symbol)}>
                  <td><button className="link" onClick={(e) => { e.stopPropagation(); onSelect(r.symbol); }}>{r.symbol}</button></td>
                  {cols.map((k) => <td key={k} className={k === "name" || k === "sector" ? "sc-text" : "num " + (k === "change_pct" || k === "gap_pct" ? (r[k] > 0 ? "up" : r[k] < 0 ? "down" : "") : "")}>{fmt(k, r[k])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
