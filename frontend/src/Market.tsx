import { Fragment, useEffect, useRef, useState } from "react";
import { Info } from "./Info";
import { api, compact, fixed, money, pct, price, qty, time, timeHeading, tone } from "./util";

export function usePoll<T>(path: string | null, ms: number, deps: any[] = []): [T | null, string, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!path) { setData(null); return; }
    let alive = true;
    const load = () => api(path).then((d) => { if (alive) { setData(d); setError(""); } }).catch((e) => alive && setError(e.message));
    load();
    const t = ms > 0 ? setInterval(load, ms) : null;
    return () => { alive = false; if (t) clearInterval(t); };
  }, [path, n, ...deps]);
  return [data, error, () => setN((x) => x + 1)];
}

export function Watchlist({ symbols, quotes, selected, onSelect, onChange }: {
  symbols: string[]; quotes: Record<string, any>; selected: string; onSelect: (s: string) => void; onChange: (s: string[]) => void;
}) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[]>([]);
  useEffect(() => {
    if (!q.trim()) { setResults([]); return; }
    let alive = true;
    api(`/symbols?q=${encodeURIComponent(q)}`).then((r) => alive && setResults(r.slice(0, 8))).catch(() => {});
    return () => { alive = false; };
  }, [q]);
  const pick = (s: string) => { if (!symbols.includes(s)) onChange([...symbols, s]); onSelect(s); setQ(""); };
  return (
    <section className="panel watch" aria-label="Watchlist">
      <h3>Watchlist</h3>
      <div className="search">
        <input value={q} placeholder="Search symbol or company" aria-label="Search symbols" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && results[0]) pick(results[0].symbol); if (e.key === "Escape") setQ(""); }} />
        {q.trim() && (
          <ul className="results">
            {results.length === 0 && <li className="muted">No matching symbol</li>}
            {results.map((r) => (
              <li key={r.symbol}><button onClick={() => pick(r.symbol)}><b>{r.symbol}</b><span className="muted">{r.name}</span></button></li>
            ))}
          </ul>
        )}
      </div>
      <div className="scroll">
        <table className="tight">
          <thead><tr><th>Symbol</th><th className="num">Last</th><th className="num">Chg %</th><th /></tr></thead>
          <tbody>
            {symbols.map((s) => {
              const x = quotes[s];
              return (
                <tr key={s} className={s === selected ? "sel click" : "click"} onClick={() => onSelect(s)}>
                  <td><b>{s}</b></td>
                  <td className="num">{money(x?.last)}</td>
                  <td className={"num " + tone(x?.change_pct)}>{pct(x?.change_pct)}</td>
                  <td className="num"><button className="x" title={`Remove ${s}`} aria-label={`Remove ${s}`}
                    onClick={(e) => { e.stopPropagation(); onChange(symbols.filter((y) => y !== s)); }}>×</button></td>
                </tr>
              );
            })}
            {symbols.length === 0 && <tr><td colSpan={4} className="muted">Search above to add symbols.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function QuoteHeader({ symbol, quote, info }: { symbol: string; quote: any; info: any }) {
  const cell = (label: string, value: string) => <div><span>{label}</span><b>{value}</b></div>;
  return (
    <div className="quote-head">
      <div className="qh-main">
        <h2>{symbol}</h2>
        <span className="muted">{info?.name}{info?.sector ? ` · ${info.sector}` : ""}</span>
        <div className="qh-price">
          <strong>{money(quote?.last)}</strong>
          <span className={tone(quote?.change)}>{quote ? `${quote.change > 0 ? "+" : ""}${money(quote.change)} (${pct(quote.change_pct)})` : ""}</span>
        </div>
      </div>
      <div className="qh-grid">
        {cell("Bid", quote ? `${money(quote.bid)} × ${qty(quote.bid_size)}` : "–")}
        {cell("Ask", quote ? `${money(quote.ask)} × ${qty(quote.ask_size)}` : "–")}
        {cell("Open", money(quote?.open))}
        {cell("Prev close", money(quote?.prev_close))}
        {cell("Day range", quote ? `${money(quote.low)} – ${money(quote.high)}` : "–")}
        {cell("Volume", compact(quote?.volume))}
        {cell("52-wk range", info ? `${money(info.low_52w)} – ${money(info.high_52w)}` : "–")}
        {cell("Mkt cap", compact(info?.market_cap))}
      </div>
    </div>
  );
}

export function DepthTape({ symbol, note }: { symbol: string; note?: string }) {
  const [depth, depthError] = usePoll<any>(`/depth/${symbol}`, 1500);
  const [trades, tradesError] = usePoll<any[]>(`/trades/${symbol}?limit=40`, 1500);
  const max = depth ? Math.max(1, ...depth.bids.map((b: any) => b.size), ...depth.asks.map((a: any) => a.size)) : 1;
  return (
    <section className="panel depth" aria-label="Depth and trades">
      <h3>Depth{note ? `: ${note}` : ""} <span className="tag">simulated depth</span></h3>
      {depthError && !depth && <p className="muted pad">Could not load the depth: {depthError}</p>}
      {!depth && !depthError && <p className="muted pad">Loading depth…</p>}
      <div className="ladder">
        <table className="tight">
          <thead><tr><th className="num">Size</th><th className="num">Bid</th><th>Ask</th><th>Size</th></tr></thead>
          <tbody>
            {(depth?.bids || []).slice(0, 8).map((b: any, i: number) => {
              const a = depth.asks[i];
              return (
                <tr key={i}>
                  <td className="num bar-cell"><i className="bar bid" style={{ width: `${(b.size / max) * 100}%` }} />{qty(b.size)}</td>
                  <td className="num up">{money(b.price)}</td>
                  <td className="down">{money(a?.price)}</td>
                  <td className="bar-cell"><i className="bar ask" style={{ width: `${((a?.size || 0) / max) * 100}%` }} />{qty(a?.size)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h3>Time and sales</h3>
      {tradesError && !trades && <p className="muted pad">Could not load recent trades: {tradesError}</p>}
      <div className="scroll">
        <table className="tight">
          <thead><tr><th>{timeHeading()}</th><th className="num">Price</th><th className="num">Size</th></tr></thead>
          <tbody>
            {(trades || []).map((t, i) => (
              <tr key={i}><td className="muted">{time(t.ts)}</td>
                <td className={"num " + (t.side === "buy" ? "up" : t.side === "sell" ? "down" : "")}>{money(t.price)}</td>
                <td className="num">{qty(t.size)}</td></tr>
            ))}
            {trades && trades.length === 0 && <tr><td colSpan={3} className="muted">No trades yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function OptionChain({ symbol, onTrade }: { symbol: string; onTrade: (optSymbol: string, side: string, price: number) => void }) {
  const [exps] = usePoll<any[]>(`/options/expirations/${symbol}`, 0);
  const [expiry, setExpiry] = useState("");
  useEffect(() => { setExpiry(""); }, [symbol]);
  const chosen = expiry || (exps && exps[1]?.date) || (exps && exps[0]?.date) || "";
  const [chain, error] = usePoll<any>(chosen ? `/options/chain/${symbol}?expiry=${chosen}` : null, 3000);
  // Scroll the at-the-money strike into view once per symbol and expiry (not on every 3 s refresh).
  const tableRef = useRef<HTMLTableElement>(null);
  const scrolledFor = useRef("");
  useEffect(() => {
    const key = `${symbol}|${chosen}`;
    if (!chain?.rows?.length || scrolledFor.current === key || !tableRef.current) return;
    scrolledFor.current = key;
    const spotRow = tableRef.current.querySelector("tr.spot-row") as HTMLElement | null;
    const box = tableRef.current.closest(".scroll") as HTMLElement | null;
    if (!spotRow) return;
    // Centre it in the chain's own scroller, or in the phone view that scrolls as a whole.
    const scroller = box && box.scrollHeight > box.clientHeight + 1 ? box : (tableRef.current.closest(".m-chart") as HTMLElement | null);
    if (!scroller) return;
    const top = spotRow.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    scroller.scrollTop = Math.max(0, top - scroller.clientHeight / 2);
  }, [chain, symbol, chosen]);
  const spotAt = chain?.rows?.length ? chain.rows.findIndex((r: any) => r.strike >= chain.spot) : -1;
  const cells = (o: any, itm: boolean, order: string[]) => {
    const c = itm ? " itm" : "";
    const all: Record<string, JSX.Element> = {
      oi: <td key="oi" className={"num muted" + c}>{compact(o.open_interest)}</td>,
      vol: <td key="vol" className={"num muted" + c}>{compact(o.volume)}</td>,
      iv: <td key="iv" className={"num" + c}>{(o.iv * 100).toFixed(1)}%</td>,
      delta: <td key="delta" className={"num" + c}>{fixed(o.delta)}</td>,
      bid: <td key="bid" className={"num" + c}><button className="px down" disabled={!(o.bid > 0)} title={o.bid > 0 ? "Sell at the bid" : "No bid: nobody is buying this contract"}
        onClick={() => onTrade(o.symbol, "sell", o.bid)}>{money(o.bid)}</button></td>,
      ask: <td key="ask" className={"num" + c}><button className="px up" disabled={!(o.ask > 0)} title={o.ask > 0 ? "Buy at the ask" : "No ask: nobody is selling this contract"}
        onClick={() => onTrade(o.symbol, "buy", o.ask)}>{money(o.ask)}</button></td>,
    };
    return order.map((k) => all[k]);
  };
  return (
    <div className="chain">
      <div className="chart-bar">
        <label className="inline">Expiry
          <select value={chosen} onChange={(e) => setExpiry(e.target.value)}>
            {(exps || []).map((e) => <option key={e.date} value={e.date}>{expiryLabel(e.date)} ({e.dte === 0 ? "today" : e.dte === 1 ? "1 day" : `${e.dte} days`})</option>)}
          </select>
        </label>
        <span className="tag">{chain?.source_label || "model prices, not market data"}{chain?.model_legs ? ` · ${chain.model_legs} model-priced` : ""}</span>
        <span className="muted">Click a bid to sell or an ask to buy. Each contract covers 100 shares. Shaded rows are in the money.</span>
      </div>
      {error && <div className="notice bad">Could not load the option chain: {error}</div>}
      <div className="scroll">
        <table className="tight chain-table" ref={tableRef}>
          <thead>
            <tr><th colSpan={6} className="center">Calls</th><th /><th colSpan={6} className="center">Puts</th></tr>
            <tr>
              <th className="num">OI <Info term="oi" /></th><th className="num">Vol</th><th className="num">IV <Info term="iv" /></th><th className="num">Delta <Info term="delta" /></th><th className="num">Bid</th><th className="num">Ask</th>
              <th className="center">Strike</th>
              <th className="num">Bid</th><th className="num">Ask</th><th className="num">Delta</th><th className="num">IV</th><th className="num">Vol</th><th className="num">OI</th>
            </tr>
          </thead>
          <tbody>
            {(chain?.rows || []).map((r: any, i: number) => (
              <Fragment key={r.strike}>
                {i === spotAt && (
                  <tr className="spot-row"><td colSpan={13}><span>{chain.symbol || symbol} last {price(chain.spot)}</span></td></tr>
                )}
                <tr>
                  {cells(r.call, r.strike < chain.spot, ["oi", "vol", "iv", "delta", "bid", "ask"])}
                  <td className="center strike">{money(r.strike, r.strike % 1 ? 1 : 0)}</td>
                  {cells(r.put, r.strike > chain.spot, ["bid", "ask", "delta", "iv", "vol", "oi"])}
                </tr>
              </Fragment>
            ))}
            {spotAt === -1 && chain?.rows?.length > 0 && (
              <tr className="spot-row"><td colSpan={13}><span>{chain.symbol || symbol} last {price(chain.spot)}</span></td></tr>
            )}
          </tbody>
        </table>
        {!chain && !error && <p className="muted pad">Loading option chain…</p>}
        {chain && !chain.rows?.length && <p className="muted pad">No contracts are listed for this expiry.</p>}
      </div>
    </div>
  );
}

export function Company({ info }: { info: any }) {
  if (!info) return <p className="muted pad">Loading company data…</p>;
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  const etf = info.type === "etf";
  return (
    <div className="company">
      {info.simulated !== false ? <div className="notice">Every company here is fictional and its figures are made up. To see real companies, connect a market data provider (see docs/DATA_PROVIDERS.md).</div>
        : <div className="notice">These figures are computed from price data only. Earnings, dividends and fundamentals from the data provider are on the Research tab.</div>}
      <div className="facts">
        {row("Name", info.name)}
        {row("Sector", info.sector)}
        {row("Type", etf ? "ETF (a fund that trades like a stock)" : "Stock")}
        {row(etf ? "Fund size" : "Market cap", compact(info.market_cap))}
        {!etf && row("Shares outstanding", compact(info.shares_outstanding))}
        {!etf && row("Earnings per share", price(info.eps))}
        {!etf && row("Price / earnings", info.pe == null ? "–" : info.pe.toFixed(1))}
        {row("Dividend per share (yearly)", price(info.dividend))}
        {row("Dividend yield", info.dividend_yield_pct == null ? "–" : info.dividend_yield_pct.toFixed(2) + "%")}
        {row("52-week high", price(info.high_52w))}
        {row("52-week low", price(info.low_52w))}
        {row("Average volume (30 days)", compact(info.avg_volume_30d))}
        {row("Volatility (annual)", info.historical_volatility == null ? "–" : (info.historical_volatility * 100).toFixed(0) + "%")}
      </div>
    </div>
  );
}

/** "Fri 4 Dec 2026" for an ISO date, read as a calendar date (no time zone shift). */
function expiryLabel(iso: string) {
  const d = new Date(iso + "T12:00:00Z");
  return isNaN(+d) ? iso : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
