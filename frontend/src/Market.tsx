import { Fragment, useEffect, useRef, useState } from "react";
import { Info } from "./Info";
import { api, ASSET_LABELS, compact, fixed, isCrypto, money, pct, price, qty, time, timeHeading, tone } from "./util";
import { KeyRatios } from "./ResearchViews";
import "./Research.css";
import { describeLeg, legFromSymbol, parseOption } from "./payoff";

const describeContract = (s: string) => describeLeg(parseOption(s)?.underlying || s, legFromSymbol(s, "buy"));

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
      <div className="qh-grid" tabIndex={0} role="group" aria-label="Quote details">
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

export function DepthTape({ symbol, note }: { symbol: string; note?: string }) {
  const [depth, depthError] = usePoll<any>(`/depth/${symbol}`, 1500);
  const [trades, tradesError] = usePoll<any[]>(`/trades/${symbol}?limit=40`, 1500);
  const max = depth ? Math.max(1, ...depth.bids.map((b: any) => b.size), ...depth.asks.map((a: any) => a.size)) : 1;
  return (
    <section className="panel depth" aria-label="Depth and trades">
      <h3>Depth{note ? `: ${note}` : ""} <span className="tag">{depth && depth.simulated === false ? `order book · ${depth.source || "provider"}` : "simulated depth"}</span></h3>
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
      {tradesError && !trades && <p className="muted pad">Could not load recent trades: {tradesError}</p>}
      <div className="scroll" tabIndex={0} role="group" aria-label="Time and sales">
        <table className="tight">
          <thead><tr><th>{timeHeading()}</th><th className="num">Price</th><th className="num">Size</th></tr></thead>
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

export type StrategyPick = { symbol: string; side: "buy" | "sell"; ratio: number };

export function OptionChain({ symbol, onTrade, onStrategy }: {
  symbol: string; onTrade: (optSymbol: string, side: string, price: number) => void;
  onStrategy?: (legs?: StrategyPick[]) => void; // opens the strategy builder, empty or with legs picked in the chain
}) {
  const [exps] = usePoll<any[]>(`/options/expirations/${symbol}`, 0);
  const [expiry, setExpiry] = useState("");
  // "Add to strategy" mode: a click on a bid adds a sell leg, on an ask a buy leg, instead of opening the ticket.
  const [adding, setAdding] = useState(false);
  const [picks, setPicks] = useState<StrategyPick[]>([]);
  const [pickNote, setPickNote] = useState("");
  useEffect(() => { setExpiry(""); setPicks([]); setAdding(false); }, [symbol]);
  const pick = (s: string, side: "buy" | "sell") => {
    setPickNote("");
    const at = picks.findIndex((p) => p.symbol === s);
    if (at >= 0) setPicks(picks[at].side === side ? picks.filter((_, i) => i !== at) : picks.map((p, i) => (i === at ? { ...p, side } : p)));
    else if (picks.length >= 4) setPickNote("A strategy has at most 4 legs. Remove one first (click it again).");
    else setPicks([...picks, { symbol: s, side, ratio: 1 }]);
  };
  const picked = (s: string, side: string) => picks.some((p) => p.symbol === s && p.side === side);
  const press = (s: string, side: "buy" | "sell", price: number) => (adding ? pick(s, side) : onTrade(s, side, price));
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
      bid: <td key="bid" className={"num" + c}><button className={"px down" + (picked(o.symbol, "sell") ? " picked" : "")} disabled={!(o.bid > 0)} title={!(o.bid > 0) ? "No bid: nobody is buying this contract" : adding ? "Add a sell leg" : "Sell at the bid"}
        aria-pressed={adding ? picked(o.symbol, "sell") : undefined} onClick={() => press(o.symbol, "sell", o.bid)}>{money(o.bid)}</button></td>,
      ask: <td key="ask" className={"num" + c}><button className={"px up" + (picked(o.symbol, "buy") ? " picked" : "")} disabled={!(o.ask > 0)} title={!(o.ask > 0) ? "No ask: nobody is selling this contract" : adding ? "Add a buy leg" : "Buy at the ask"}
        aria-pressed={adding ? picked(o.symbol, "buy") : undefined} onClick={() => press(o.symbol, "buy", o.ask)}>{money(o.ask)}</button></td>,
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
        <span className="muted">{adding ? "Adding to a strategy: click an ask to add a buy leg, a bid to add a sell leg." : "Click a bid to sell or an ask to buy. Each contract covers 100 shares. Shaded rows are in the money."}</span>
        {onStrategy && (
          <span className="chain-strategy">
            <button className="ghost sm" onClick={() => onStrategy()}>Build a strategy…</button>
            <button className={"ghost sm" + (adding ? " on" : "")} aria-pressed={adding} onClick={() => { setAdding(!adding); setPickNote(""); }}>Add to strategy</button>
          </span>
        )}
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
      {adding && (
        <div className="strategy-tray" role="region" aria-label="Strategy legs picked">
          <span>{picks.length === 0 ? "No legs yet. Pick bids (sell) and asks (buy) in the chain." : picks.map((p) => `${p.side === "buy" ? "Buy" : "Sell"} ${describeContract(p.symbol)}`).join(" · ")}</span>
          {pickNote && <span className="down">{pickNote}</span>}
          <span className="strategy-tray-actions">
            {picks.length > 0 && <button className="ghost sm" onClick={() => setPicks([])}>Clear</button>}
            <button className="primary sm" disabled={!picks.length} onClick={() => onStrategy?.(picks)}>Open in strategy builder ({picks.length} leg{picks.length === 1 ? "" : "s"})</button>
          </span>
        </div>
      )}
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
  const etf = info.type === "etf";
  return (
    <div className="company">
      {info.simulated !== false ? <div className="notice">Every company in this build is fictional and its figures are simulated. Financial statements, earnings, news and calendars are on the Research tab.</div>
        : <div className="notice">These figures are computed from price data only. Earnings, dividends and fundamentals from the data provider are on the Research tab.</div>}
      <div className="facts">
        {row("Name", info.name)}
        {row("Sector", info.sector)}
        {row("Type", etf ? "ETF (a fund that trades like a stock)" : "Stock")}
        {row(etf ? "Fund size" : "Market cap", compact(sim ? sim.market_cap : info.market_cap))}
        {!etf && row("Shares outstanding", compact(sim ? sim.shares_outstanding : info.shares_outstanding))}
        {!etf && row(sim ? "Earnings per share (trailing)" : "Earnings per share", price(eps))}
        {!etf && row("Price / earnings", pe == null ? "–" : pe.toFixed(1))}
        {row("Dividend per share (yearly)", price(info.dividend))}
        {row("Dividend yield", info.dividend_yield_pct == null ? "–" : info.dividend_yield_pct.toFixed(2) + "%")}
        {row("52-week high", price(info.high_52w))}
        {row("52-week low", price(info.low_52w))}
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

/** "Fri 4 Dec 2026" for an ISO date, read as a calendar date (no time zone shift). */
function expiryLabel(iso: string) {
  const d = new Date(iso + "T12:00:00Z");
  return isNaN(+d) ? iso : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
