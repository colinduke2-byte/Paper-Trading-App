import { useEffect, useState } from "react";
import { api, isOption, money, ORDER_TYPES, qty as fmtQty, TIFS, usd } from "./util";

export type TicketSeed = { symbol: string; side?: string; qty?: number; type?: string; limit_price?: number; nonce?: number };

export function Ticket({ seed, accountId, quote, description, position, onPlaced }: {
  seed: TicketSeed; accountId: string; quote: any; description: string; position: any; onPlaced: (msg: string) => void;
}) {
  const [side, setSide] = useState("buy");
  const [qty, setQty] = useState("");
  const [type, setType] = useState("market");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [trailMode, setTrailMode] = useState("amount");
  const [trail, setTrail] = useState("");
  const [tif, setTif] = useState("day");
  const [ext, setExt] = useState(false);
  const [bracket, setBracket] = useState(false);
  const [tp, setTp] = useState("");
  const [sl, setSl] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const option = isOption(seed.symbol);

  useEffect(() => {
    setSide(seed.side || "buy");
    setQty(seed.qty ? String(seed.qty) : option ? "1" : "");
    setType(seed.type || "market");
    setLimit(seed.limit_price != null ? String(seed.limit_price) : "");
    setStop(""); setTrail(""); setTp(""); setSl(""); setBracket(false); setExt(false); setTif("day");
    setPreview(null); setError("");
  }, [seed.symbol, seed.nonce]);

  const hasLimit = type === "limit" || type === "stop_limit";
  const hasStop = type === "stop" || type === "stop_limit";
  const canBracket = !option && (type === "market" || type === "limit") && (tif === "day" || tif === "gtc");
  const immediateOk = type === "market" || type === "limit";

  const body = () => ({
    symbol: seed.symbol, side, qty, type, tif,
    limit_price: hasLimit ? limit : null,
    stop_price: hasStop ? stop : null,
    trail_amount: type === "trailing_stop" && trailMode === "amount" ? trail : null,
    trail_percent: type === "trailing_stop" && trailMode === "percent" ? trail : null,
    extended_hours: ext && type === "limit",
    take_profit: bracket && canBracket && tp ? tp : null,
    stop_loss: bracket && canBracket && sl ? sl : null,
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
        ? `Filled: ${o.side} ${fmtQty(o.filled_qty)} ${description} at ${money(o.avg_fill_price, o.avg_fill_price < 1 ? 4 : 2)}`
        : `Order placed: ${o.side} ${fmtQty(o.qty)} ${description} (${o.status.replace("_", " ")})`);
      setPreview(null);
      if (!option) setQty("");
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const sideLabel = option
    ? side === "buy" ? (position && position.qty < 0 ? "Buy to close" : "Buy to open") : position && position.qty > 0 ? "Sell to close" : "Sell to open"
    : side === "buy" ? (position && position.qty < 0 ? "Buy to cover" : "Buy") : position && position.qty > 0 ? "Sell" : "Sell short";

  return (
    <section className="panel ticket" aria-label="Order ticket">
      <h3>Order ticket</h3>
      <div className="ticket-sym">
        <strong>{description}</strong>
        {quote ? (
          <span className="muted">Bid {money(quote.bid)} × {fmtQty(quote.bid_size)} · Ask {money(quote.ask)} × {fmtQty(quote.ask_size)}</span>
        ) : <span className="muted">No quote</span>}
        {position && <span className="muted">Position: {fmtQty(position.qty)} @ {money(position.avg_cost)}</span>}
      </div>
      <div className="side-toggle">
        <button className={side === "buy" ? "buy on" : "buy"} onClick={() => edit(() => setSide("buy"))}>Buy</button>
        <button className={side === "sell" ? "sell on" : "sell"} onClick={() => edit(() => setSide("sell"))}>Sell</button>
      </div>
      <label>{option ? "Contracts" : "Shares"}
        <input inputMode="decimal" value={qty} placeholder="0" onChange={(e) => edit(() => setQty(e.target.value))} />
      </label>
      <label>Order type
        <select value={type} onChange={(e) => edit(() => { setType(e.target.value); if (e.target.value !== "market" && e.target.value !== "limit" && (tif === "ioc" || tif === "fok")) setTif("day"); })}>
          {Object.entries(ORDER_TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {hasStop && <label>Stop price<input inputMode="decimal" value={stop} onChange={(e) => edit(() => setStop(e.target.value))} /></label>}
      {hasLimit && <label>Limit price<input inputMode="decimal" value={limit} onChange={(e) => edit(() => setLimit(e.target.value))} /></label>}
      {type === "trailing_stop" && (
        <label>Trail by
          <span className="row">
            <input inputMode="decimal" value={trail} onChange={(e) => edit(() => setTrail(e.target.value))} />
            <select value={trailMode} onChange={(e) => edit(() => setTrailMode(e.target.value))}>
              <option value="amount">$</option><option value="percent">%</option>
            </select>
          </span>
        </label>
      )}
      <label>Time in force
        <select value={tif} onChange={(e) => edit(() => setTif(e.target.value))}>
          {Object.entries(TIFS).filter(([k]) => immediateOk || (k !== "ioc" && k !== "fok")).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {type === "limit" && !option && (
        <label className="check"><input type="checkbox" checked={ext} onChange={(e) => edit(() => setExt(e.target.checked))} />Allow extended hours</label>
      )}
      {canBracket && (
        <label className="check"><input type="checkbox" checked={bracket} onChange={(e) => edit(() => setBracket(e.target.checked))} />Attach take profit and stop loss</label>
      )}
      {bracket && canBracket && (
        <div className="row">
          <label>Take profit<input inputMode="decimal" value={tp} onChange={(e) => edit(() => setTp(e.target.value))} /></label>
          <label>Stop loss<input inputMode="decimal" value={sl} onChange={(e) => edit(() => setSl(e.target.value))} /></label>
        </div>
      )}
      {error && <div className="notice bad" role="alert">{error}</div>}
      {preview && preview.ok && (
        <div className="preview">
          <div><span>Estimated price</span><b>{money(preview.estimated_price, preview.estimated_price < 1 ? 4 : 2)}</b></div>
          <div><span>Estimated fees</span><b>{usd(preview.estimated_fees)}</b></div>
          <div><span>{side === "buy" ? "Estimated cost" : "Estimated proceeds"}</span><b>{usd(preview.estimated_total)}</b></div>
          <div><span>Position after</span><b>{fmtQty(preview.position_after)}</b></div>
          {preview.buying_power_after != null && <div><span>Buying power after</span><b>{usd(preview.buying_power_after)}</b></div>}
          {preview.warnings.map((w: string) => <p key={w} className="muted">{w}</p>)}
        </div>
      )}
      {preview && preview.ok ? (
        <div className="row">
          <button className="ghost" onClick={() => setPreview(null)} disabled={busy}>Edit</button>
          <button className={side === "buy" ? "primary buy" : "primary sell"} onClick={place} disabled={busy}>Place paper order</button>
        </div>
      ) : (
        <button className="primary" onClick={review} disabled={busy || !qty}>Review: {sideLabel}</button>
      )}
    </section>
  );
}
