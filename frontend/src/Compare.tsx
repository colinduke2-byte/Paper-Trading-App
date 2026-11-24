import { useEffect, useState } from "react";
import { usePoll } from "./Market";
import { SymbolSearch } from "./SymbolSearch";
import { compact, money, pct, store, tone } from "./util";
import "./discovery.css";

// Two to five symbols side by side: price performance over 1D to 1Y (from daily bars) and key ratios.

const load = (): string[] => { try { return JSON.parse(store("pd.compare") || "[]").filter((s: any) => typeof s === "string").slice(0, 5); } catch { return []; } };

export function Compare({ symbol, onSelect }: { symbol: string; onSelect: (s: string) => void }) {
  const [syms, setSyms] = useState<string[]>(() => { const l = load(); return l.length ? l : [symbol]; });
  useEffect(() => { store("pd.compare", JSON.stringify(syms)); }, [syms.join(",")]);
  const ready = syms.length >= 2;
  const [data, error] = usePoll<any>(ready ? `/compare?symbols=${syms.join(",")}` : null, 15000, [syms.join(",")]);
  const add = (s: string) => setSyms((x) => (x.includes(s) || x.length >= 5 ? x : [...x, s]));
  const rows: Record<string, any> = Object.fromEntries((data?.symbols || []).map((r: any) => [r.symbol, r]));
  const shown = syms.filter((s) => rows[s]);
  const best = (vals: (number | null)[]) => { const v = vals.filter((x): x is number => x != null); return v.length > 1 ? Math.max(...v) : null; };
  const line = (label: string, get: (r: any) => any, show: (v: any) => string, opts: { tone?: boolean; best?: boolean } = {}) => {
    const vals = shown.map((s) => get(rows[s]));
    const top = opts.best ? best(vals) : null;
    return (
      <tr key={label}>
        <th scope="row">{label}</th>
        {vals.map((v, i) => <td key={shown[i]} className={"num " + (opts.tone ? tone(v) : "") + (top != null && v === top ? " cmp-best" : "")}>{v == null ? "–" : show(v)}</td>)}
      </tr>
    );
  };
  return (
    <div className="compare">
      <div className="cmp-head">
        <div className="cmp-chips" role="list" aria-label="Symbols compared">
          {syms.map((s) => (
            <span key={s} className="cmp-chip" role="listitem">
              <button className="link" onClick={() => onSelect(s)}>{s}</button>
              <button className="x" aria-label={`Stop comparing ${s}`} onClick={() => setSyms((x) => x.filter((y) => y !== s))}>×</button>
            </span>
          ))}
        </div>
        {syms.length < 5 && <SymbolSearch onPick={add} label="Add a symbol to compare" placeholder="Add a symbol (2 to 5)" exclude={syms} />}
        {!syms.includes(symbol) && syms.length < 5 && <button className="ghost sm" onClick={() => add(symbol)}>Add {symbol}</button>}
      </div>
      {!ready && <p className="muted pad">Add at least one more symbol to compare.</p>}
      {error && <div className="notice bad">Could not compare these symbols: {error}</div>}
      {ready && data && (
        <div className="cmp-scroll">
          <table className="cmp-table">
            <thead><tr><th />{shown.map((s) => <th key={s} className="num" scope="col">{s}<div className="muted cmp-name">{rows[s].name}</div></th>)}</tr></thead>
            <tbody>
              {line("Last", (r) => r.last, (v) => money(v))}
              {data.periods.map((p: string) => line(`Change ${p}`, (r) => r.performance[p], pct, { tone: true, best: true }))}
              {line("Market cap", (r) => r.market_cap, compact)}
              {line("P/E", (r) => r.pe, (v) => v.toFixed(1))}
              {line("Dividend yield", (r) => r.dividend_yield, (v) => v.toFixed(2) + "%")}
              {line("52-week low", (r) => r.low_52w, (v) => money(v))}
              {line("52-week high", (r) => r.high_52w, (v) => money(v))}
              {line("Position in 52-week range", (r) => r.range_52w_pos, (v) => v.toFixed(0) + "%")}
              {line("Vs 50-day average", (r) => r.pct_vs_sma50, pct, { tone: true })}
              {line("Vs 200-day average", (r) => r.pct_vs_sma200, pct, { tone: true })}
              {line("RSI (14 days)", (r) => r.rsi14, (v) => v.toFixed(0))}
              {line("Volatility (1 year)", (r) => r.volatility, (v) => (v * 100).toFixed(0) + "%")}
              {line("Average volume (30 days)", (r) => r.avg_volume_30d, compact)}
              {line("Price source", (r) => r.quote_source, String)}
            </tbody>
          </table>
          <p className="muted pad">{data.note}{data.simulated ? " All companies here are fictional and their prices simulated." : ""}</p>
        </div>
      )}
    </div>
  );
}
