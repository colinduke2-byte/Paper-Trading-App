import { useEffect, useState } from "react";
import { api, ASSET_LABELS, compact, isCrypto, money, pct, price, qty, time, tone } from "./util";
import { KeyRatios } from "./ResearchViews";
import "./Research.css";

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
  const [cls, setCls] = useState(""); // asset-class filter: "" (all), "stock" or "crypto"
  const [results, setResults] = useState<any[]>([]);
  useEffect(() => {
    if (!q.trim() && !cls) { setResults([]); return; }
    let alive = true;
    api(`/symbols?q=${encodeURIComponent(q)}&asset_class=${cls}`).then((r) => alive && setResults(r.slice(0, 8))).catch(() => {});
    return () => { alive = false; };
  }, [q, cls]);
  const pick = (s: string) => { if (!symbols.includes(s)) onChange([...symbols, s]); onSelect(s); setQ(""); };
  return (
    <section className="panel watch" aria-label="Watchlist">
      <h3>Watchlist</h3>
      <div className="search">
        <div className="asset-filter seg" role="group" aria-label="Asset class">
          {[["", "All"], ["stock", "Stocks"], ["crypto", "Crypto"]].map(([k, label]) => (
            <button key={k} className={cls === k ? "on" : ""} aria-pressed={cls === k} onClick={() => setCls(k)}>{label}</button>
          ))}
        </div>
        <input value={q} placeholder={cls === "crypto" ? "Search coin" : "Search symbol or company"} aria-label="Search symbols" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && results[0]) pick(results[0].symbol); if (e.key === "Escape") { setQ(""); setCls(""); } }} />
        {(q.trim() || cls) && (
          <ul className="results">
            {results.length === 0 && <li className="muted">No matching symbol</li>}
            {results.map((r) => (
              <li key={r.symbol}><button onClick={() => { pick(r.symbol); setCls(""); }}><b>{r.symbol}</b><span className="muted">{r.name}</span>
                {r.asset_class === "crypto" && <span className="tag">{ASSET_LABELS.crypto}</span>}</button></li>
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
                  <td className="num">{price(x?.last)}</td>
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
        <span className="muted">{info?.name}{isCrypto(symbol) ? " · Crypto · trades 24/7" : info?.sector ? ` · ${info.sector}` : ""}</span>
        <div className="qh-price">
          <strong>{price(quote?.last)}</strong>
          <span className={tone(quote?.change)}>{quote ? `${quote.change > 0 ? "+" : ""}${price(quote.change)} (${pct(quote.change_pct)})` : ""}</span>
        </div>
      </div>
      <div className="qh-grid">
        {cell("Bid", quote ? `${price(quote.bid)} × ${qty(quote.bid_size)}` : "–")}
        {cell("Ask", quote ? `${price(quote.ask)} × ${qty(quote.ask_size)}` : "–")}
        {cell(isCrypto(symbol) ? "Open (UTC day)" : "Open", price(quote?.open))}
        {cell("Prev close", price(quote?.prev_close))}
        {cell("Day range", quote ? `${price(quote.low)} – ${price(quote.high)}` : "–")}
        {cell("Volume", compact(quote?.volume))}
        {cell("52-wk range", info ? `${price(info.low_52w)} – ${price(info.high_52w)}` : "–")}
        {isCrypto(symbol) ? cell("Asset class", "Crypto") : cell("Mkt cap", compact(info?.market_cap))}
      </div>
    </div>
  );
}

export function DepthTape({ symbol }: { symbol: string }) {
  const [depth] = usePoll<any>(`/depth/${symbol}`, 1500);
  const [trades] = usePoll<any[]>(`/trades/${symbol}?limit=40`, 1500);
  const max = depth ? Math.max(1, ...depth.bids.map((b: any) => b.size), ...depth.asks.map((a: any) => a.size)) : 1;
  return (
    <section className="panel depth" aria-label="Depth and trades">
      <h3>Depth <span className="tag">{depth && depth.simulated === false ? `order book · ${depth.source || "provider"}` : "simulated depth"}</span></h3>
      <div className="ladder">
        <table className="tight">
          <thead><tr><th className="num">Size</th><th className="num">Bid</th><th>Ask</th><th>Size</th></tr></thead>
          <tbody>
            {(depth?.bids || []).slice(0, 8).map((b: any, i: number) => {
              const a = depth.asks[i];
              return (
                <tr key={i}>
                  <td className="num bar-cell"><i className="bar bid" style={{ width: `${(b.size / max) * 100}%` }} />{qty(b.size)}</td>
                  <td className="num up">{price(b.price)}</td>
                  <td className="down">{price(a?.price)}</td>
                  <td className="bar-cell"><i className="bar ask" style={{ width: `${((a?.size || 0) / max) * 100}%` }} />{qty(a?.size)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <h3>Time and sales</h3>
      <div className="scroll">
        <table className="tight">
          <thead><tr><th>Time</th><th className="num">Price</th><th className="num">Size</th></tr></thead>
          <tbody>
            {(trades || []).map((t, i) => (
              <tr key={i}><td className="muted">{time(t.ts)}</td>
                <td className={"num " + (t.side === "buy" ? "up" : t.side === "sell" ? "down" : "")}>{price(t.price)}</td>
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
  const cells = (o: any, itm: boolean, order: string[]) => {
    const c = itm ? " itm" : "";
    const all: Record<string, JSX.Element> = {
      oi: <td key="oi" className={"num muted" + c}>{compact(o.open_interest)}</td>,
      vol: <td key="vol" className={"num muted" + c}>{compact(o.volume)}</td>,
      iv: <td key="iv" className={"num" + c}>{(o.iv * 100).toFixed(1)}%</td>,
      delta: <td key="delta" className={"num" + c}>{o.delta.toFixed(2)}</td>,
      bid: <td key="bid" className={"num" + c}><button className="px down" title="Sell at the bid" onClick={() => onTrade(o.symbol, "sell", o.bid)}>{money(o.bid)}</button></td>,
      ask: <td key="ask" className={"num" + c}><button className="px up" title="Buy at the ask" onClick={() => onTrade(o.symbol, "buy", o.ask)}>{money(o.ask)}</button></td>,
    };
    return order.map((k) => all[k]);
  };
  return (
    <div className="chain">
      <div className="chart-bar">
        <label className="inline">Expiry
          <select value={chosen} onChange={(e) => setExpiry(e.target.value)}>
            {(exps || []).map((e) => <option key={e.date} value={e.date}>{e.date} ({e.dte}d)</option>)}
          </select>
        </label>
        <span className="tag">{chain?.source_label || "model prices, not market data"}{chain?.model_legs ? ` · ${chain.model_legs} model-priced` : ""}</span>
        <span className="muted">Click a bid to sell or an ask to buy. Each contract covers 100 shares.</span>
      </div>
      {error && <div className="notice bad">Could not load the option chain: {error}</div>}
      <div className="scroll">
        <table className="tight chain-table">
          <thead>
            <tr><th colSpan={6} className="center">Calls</th><th /><th colSpan={6} className="center">Puts</th></tr>
            <tr>
              <th className="num">OI</th><th className="num">Vol</th><th className="num">IV</th><th className="num">Delta</th><th className="num">Bid</th><th className="num">Ask</th>
              <th className="center">Strike</th>
              <th className="num">Bid</th><th className="num">Ask</th><th className="num">Delta</th><th className="num">IV</th><th className="num">Vol</th><th className="num">OI</th>
            </tr>
          </thead>
          <tbody>
            {(chain?.rows || []).map((r: any) => (
              <tr key={r.strike}>
                {cells(r.call, r.strike < chain.spot, ["oi", "vol", "iv", "delta", "bid", "ask"])}
                <td className="center strike">{money(r.strike, r.strike % 1 ? 1 : 0)}</td>
                {cells(r.put, r.strike > chain.spot, ["bid", "ask", "delta", "iv", "vol", "oi"])}
              </tr>
            ))}
          </tbody>
        </table>
        {!chain && !error && <p className="muted pad">Loading option chain…</p>}
      </div>
    </div>
  );
}

export function Company({ info }: { info: any }) {
  // Simulated research (fictional companies) has statements behind these figures; use them when present.
  const [env] = usePoll<any>(info?.symbol ? `/research/fundamentals/${info.symbol}` : null, 60000);
  if (!info) return <p className="muted pad">Loading company data…</p>;
  const sim = env?.available && env.data?.simulated ? env.data : null;
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  const eps = sim ? sim.eps : info.eps, pe = sim ? sim.pe : info.pe;
  return (
    <div className="company">
      {info.simulated !== false ? <div className="notice">Every company in this build is fictional and its figures are simulated. Financial statements, earnings, news and calendars are on the Research tab.</div>
        : <div className="notice">These figures are computed from price data only. Earnings, dividends and fundamentals from the data provider are on the Research tab.</div>}
      <div className="facts">
        {row("Name", info.name)}
        {row("Sector", info.sector)}
        {row("Type", info.type === "etf" ? "Fund" : "Stock")}
        {row("Market cap", compact(sim ? sim.market_cap : info.market_cap))}
        {row("Shares outstanding", compact(sim ? sim.shares_outstanding : info.shares_outstanding))}
        {info.type !== "etf" && row(sim ? "Earnings per share (trailing)" : "Earnings per share", money(eps))}
        {info.type !== "etf" && row("Price / earnings", pe == null ? "–" : pe.toFixed(1))}
        {row("Dividend per share", money(info.dividend))}
        {row("Dividend yield", info.dividend_yield_pct == null ? "–" : info.dividend_yield_pct.toFixed(2) + "%")}
        {row("52-week high", money(info.high_52w))}
        {row("52-week low", money(info.low_52w))}
        {row("Average volume (30 days)", compact(info.avg_volume_30d))}
        {row("Volatility (annual)", info.historical_volatility == null ? "–" : (info.historical_volatility * 100).toFixed(0) + "%")}
      </div>
      {sim && (
        <section className="rs-block company-ratios" aria-labelledby="co-ratios">
          <h3 id="co-ratios">{sim.kind === "fund" ? "Fund details" : `Key ratios (${sim.last_period}, trailing four quarters)`}</h3>
          <div className="rs-source">Source: Simulated (fictional companies and events)</div>
          <KeyRatios f={sim} />
        </section>
      )}
    </div>
  );
}
