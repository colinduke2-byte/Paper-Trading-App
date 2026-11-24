import { useEffect, useState } from "react";
import { usePoll } from "./Market";
import { api, price, qty as fmtQty, usd } from "./util";
import type { TicketSeed } from "./Ticket";
import "./crypto.css";

// Order ticket for crypto pairs (BASE/USD). Crypto trades 24/7, is fully paid (no margin, no short selling),
// takes a quantity in coins or a dollar amount, and allows market, limit, stop and stop-limit orders that are
// day, good-till-canceled or immediate-or-cancel. The server enforces every rule; this form only guides.
const TYPES: Record<string, string> = { market: "Market", limit: "Limit", stop: "Stop", stop_limit: "Stop limit" };
const TIFS: Record<string, string> = { day: "Day (until midnight UTC)", gtc: "Good till canceled", ioc: "Immediate or cancel" };

export function CryptoTicket({ seed, accountId, quote, position, summary, onPlaced }: {
  seed: TicketSeed; accountId: string; quote: any; position: any; summary: any; onPlaced: (msg: string) => void;
}) {
  const symbol = seed.symbol;
  const base = symbol.split("/")[0];
  const [info] = usePoll<any>(`/fundamentals/${symbol}`, 0, [symbol]);
  const [side, setSide] = useState("buy");
  const [mode, setMode] = useState<"qty" | "amount">("qty");
  const [amount, setAmount] = useState("");
  const [type, setType] = useState("market");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [tif, setTif] = useState("gtc");
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setSide(seed.side || "buy");
    setMode("qty");
    setAmount(seed.qty ? String(seed.qty) : "");
    setType(seed.type && TYPES[seed.type] ? seed.type : "market");
    setLimit(seed.limit_price != null ? String(seed.limit_price) : "");
    setStop(""); setTif("gtc"); setPreview(null); setError("");
  }, [symbol, seed.nonce]);

  const hasLimit = type === "limit" || type === "stop_limit";
  const hasStop = type === "stop" || type === "stop_limit";
  const amountOk = type === "market" || type === "limit"; // a dollar amount needs a price to convert at
  const byAmount = mode === "amount" && amountOk;

  const body = () => ({
    symbol, side, type, tif,
    qty: byAmount ? null : amount, notional: byAmount ? amount : null,
    limit_price: hasLimit ? limit : null, stop_price: hasStop ? stop : null,
  });
  const edit = (fn: () => void) => { fn(); setPreview(null); setError(""); };

  const review = async () => {
    setBusy(true); setError("");
    try {
      const p = await api(`/accounts/${accountId}/orders/preview`, { method: "POST", body: body() });
      setPreview(p);
      if (!p.ok) setError(`This order would be rejected: ${p.reason}.`);
    } catch (e: any) { setError(e.message); setPreview(null); }
    setBusy(false);
  };

  const place = async () => {
    setBusy(true); setError("");
    try {
      const orders = await api(`/accounts/${accountId}/orders`, {
        method: "POST", body: { ...body(), client_id: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` },
      });
      const o = orders[0];
      onPlaced(o.status === "filled"
        ? `Filled: ${o.side} ${fmtQty(o.filled_qty)} ${symbol} at ${price(o.avg_fill_price)} (fee ${usd(o.fees)})`
        : `Order placed: ${o.side} ${fmtQty(o.qty)} ${symbol} (${o.status.replace("_", " ")})`);
      setPreview(null); setAmount("");
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const held = position?.qty || 0;
  return (
    <section className="panel ticket crypto-ticket" aria-label="Crypto order ticket">
      <h3>Order ticket <span className="tag">Crypto · 24/7</span></h3>
      <div className="ticket-sym">
        <strong>{symbol}</strong>
        {info?.name && <span className="muted">{info.name}</span>}
        {quote ? <span className="muted">Bid {price(quote.bid)} × {fmtQty(quote.bid_size)} · Ask {price(quote.ask)} × {fmtQty(quote.ask_size)}</span>
          : <span className="muted">No quote</span>}
        {position && <span className="muted">Holding: {fmtQty(held)} {base} @ {price(position.avg_cost)}</span>}
        {summary?.crypto_buying_power != null && <span className="muted">Crypto buying power: {usd(summary.crypto_buying_power)}</span>}
      </div>
      <div className="side-toggle">
        <button className={side === "buy" ? "buy on" : "buy"} onClick={() => edit(() => setSide("buy"))}>Buy</button>
        <button className={side === "sell" ? "sell on" : "sell"} onClick={() => edit(() => setSide("sell"))}>Sell</button>
      </div>
      <div className="seg crypto-mode" role="group" aria-label="Enter quantity or amount">
        <button className={!byAmount ? "on" : ""} onClick={() => edit(() => setMode("qty"))}>Quantity ({base})</button>
        <button className={byAmount ? "on" : ""} disabled={!amountOk} title={amountOk ? undefined : "Stop orders take a quantity"}
          onClick={() => edit(() => setMode("amount"))}>Amount (USD)</button>
      </div>
      <label>{byAmount ? "Amount in US dollars (before the fee)" : `Quantity in ${base}`}
        <input inputMode="decimal" value={amount} placeholder="0" aria-label={byAmount ? "Amount in US dollars" : `Quantity in ${base}`}
          onChange={(e) => edit(() => setAmount(e.target.value))} />
      </label>
      {info && (
        <p className="muted small">Minimum {fmtQty(Number(info.min_order_size))} {base}, in steps of {fmtQty(Number(info.qty_increment))};
          prices in steps of {info.price_increment}.{side === "sell" && held > 0 ? ` You can sell up to ${fmtQty(held)}.` : ""}</p>
      )}
      <label>Order type
        <select value={type} onChange={(e) => edit(() => { setType(e.target.value); if (e.target.value !== "market" && e.target.value !== "limit" && tif === "ioc") setTif("gtc"); })}>
          {Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {hasStop && <label>Stop price<input inputMode="decimal" value={stop} onChange={(e) => edit(() => setStop(e.target.value))} /></label>}
      {hasLimit && <label>Limit price<input inputMode="decimal" value={limit} onChange={(e) => edit(() => setLimit(e.target.value))} /></label>}
      <label>Time in force
        <select value={tif} onChange={(e) => edit(() => setTif(e.target.value))}>
          {Object.entries(TIFS).filter(([k]) => k !== "ioc" || amountOk).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {error && <div className="notice bad" role="alert">{error}</div>}
      {preview && preview.ok && (
        <div className="preview">
          <div><span>Quantity</span><b>{fmtQty(preview.qty)} {base}</b></div>
          <div><span>Estimated price</span><b>{price(preview.estimated_price)}</b></div>
          <div><span>Estimated fee ({preview.fee_rate}%)</span><b>{usd(preview.estimated_fees)}</b></div>
          <div><span>{side === "buy" ? "Estimated cost" : "Estimated proceeds"}</span><b>{usd(preview.estimated_total)}</b></div>
          <div><span>Holding after</span><b>{fmtQty(preview.position_after)} {base}</b></div>
          {preview.warnings.map((w: string) => <p key={w} className="muted">{w}</p>)}
        </div>
      )}
      {preview && preview.ok ? (
        <div className="row">
          <button className="ghost" onClick={() => setPreview(null)} disabled={busy}>Edit</button>
          <button className={side === "buy" ? "primary buy" : "primary sell"} onClick={place} disabled={busy}>Place paper order</button>
        </div>
      ) : (
        <button className="primary" onClick={review} disabled={busy || !amount}>Review: {side === "buy" ? "Buy" : "Sell"} {base}</button>
      )}
    </section>
  );
}

// "About" view for a crypto pair (the Company tab of a stock).
export function CryptoAbout({ info }: { info: any }) {
  if (!info) return <p className="muted pad">Loading pair details…</p>;
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  return (
    <div className="company">
      {info.simulated !== false
        ? <div className="notice">This coin is fictional and its prices are simulated. They are not market data.</div>
        : <div className="notice">Crypto prices from the data provider. Crypto trades 24 hours a day, every day.</div>}
      <div className="facts">
        {row("Pair", info.symbol)}
        {row("Name", info.name)}
        {row("Asset class", "Crypto (fully paid: no margin, no short selling)")}
        {row("Minimum order", `${fmtQty(Number(info.min_order_size))} ${info.base || ""}`)}
        {row("Quantity step", fmtQty(Number(info.qty_increment)))}
        {row("Price step", String(info.price_increment))}
        {row("52-week high", price(info.high_52w))}
        {row("52-week low", price(info.low_52w))}
        {row("Volatility (annual)", info.historical_volatility == null ? "–" : (info.historical_volatility * 100).toFixed(0) + "%")}
      </div>
    </div>
  );
}
