import { useEffect, useState } from "react";
import { api, isOption, money, numText, ORDER_TYPES, qty as fmtQty, TIFS, usd } from "./util";
import { LotChoice, LotChoiceItem } from "./Lots";
import { sumQty } from "./lotmath";
import { LinkedOrders, LinkMode, linkedBody, newSubOrder, SubOrder, subRequest } from "./LinkedOrders";
import { AUCTION_TYPES, auctionWhich, isAuction, isoDay, ROLE_LABELS, TYPE_LABELS } from "./orderText";
import "./orders.css";

// note: shown under the limit price while it is still the seeded one (why the ticket chose that price).
// lots: tax lots to close (Lots tab, "Sell selected lots").
export type TicketSeed = { symbol: string; side?: string; qty?: number; type?: string; limit_price?: number; stop_price?: number; nonce?: number; note?: string; lots?: LotChoiceItem[] };

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
  const [gtd, setGtd] = useState(isoDay(7));
  const [link, setLink] = useState<LinkMode>("none");
  const [subs, setSubs] = useState<SubOrder[]>([]);
  const [subsOco, setSubsOco] = useState(true);
  const [auctionHint, setAuctionHint] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [lots, setLots] = useState<LotChoiceItem[] | null>(null); // chosen tax lots (Lots.tsx); null: the account's method
  const option = isOption(seed.symbol);

  useEffect(() => {
    setSide(seed.side || "buy");
    setQty(seed.qty ? String(seed.qty) : option ? "1" : "");
    setType(seed.type || "market");
    setLimit(seed.limit_price != null ? String(seed.limit_price) : "");
    setStop(seed.stop_price != null ? String(seed.stop_price) : ""); setTrail(""); setTp(""); setSl(""); setBracket(false); setExt(false); setTif("day");
    setGtd(isoDay(7)); setLink("none"); setSubs([]); setSubsOco(true);
    setPreview(null); setError(""); setLots(seed.lots?.length ? seed.lots : null);
  }, [seed.symbol, seed.nonce]);

  const auction = isAuction(type);
  const hasLimit = type === "limit" || type === "stop_limit" || type === "limit_on_open" || type === "limit_on_close";
  const hasStop = type === "stop" || type === "stop_limit";
  const linked = link !== "none";
  const canBracket = !option && !linked && (type === "market" || type === "limit") && (tif === "day" || tif === "gtc" || tif === "gtd");
  const immediateOk = (type === "market" || type === "limit") && !linked;
  const types = Object.keys(ORDER_TYPES).concat(option ? [] : AUCTION_TYPES);

  const single = () => ({
    symbol: seed.symbol, side, qty: numText(qty), type,
    ...(auction ? {} : { tif }),
    good_till_date: !auction && tif === "gtd" ? gtd : null,
    limit_price: hasLimit ? numText(limit) : null,
    stop_price: hasStop ? numText(stop) : null,
    trail_amount: type === "trailing_stop" && trailMode === "amount" ? numText(trail) : null,
    trail_percent: type === "trailing_stop" && trailMode === "percent" ? numText(trail) : null,
    extended_hours: ext && type === "limit" && !linked,
    take_profit: bracket && canBracket && tp ? numText(tp) : null,
    stop_loss: bracket && canBracket && sl ? numText(sl) : null,
    ...(lots && !bracket && !linked ? { lots: lots.map((l) => ({ ...l, qty: numText(String(l.qty)) })) } : {}),
  });
  const body = () => linkedBody(link, single(), subs, subsOco, seed.symbol);

  const edit = (fn: () => void) => { fn(); setPreview(null); setError(""); };

  // On-open and on-close orders: say when they trade, in the words the preview uses (the server knows
  // whether the simulated market ever closes). Asked for in the background once the order is complete.
  useEffect(() => {
    setAuctionHint("");
    if (!auction || !accountId || (hasLimit && !limit)) return;
    let alive = true;
    const t = setTimeout(() => {
      api(`/accounts/${accountId}/orders/preview`, { method: "POST", body: { ...single(), qty: qty || "1", take_profit: null, stop_loss: null } })
        .then((p) => { if (alive) setAuctionHint((p.warnings || []).find((w: string) => w.startsWith("This order waits")) || ""); })
        .catch(() => {});
    }, 350);
    return () => { alive = false; clearTimeout(t); };
  }, [auction, type, side, limit, seed.symbol, accountId]);

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
      const more = orders.length > 1 ? `, with ${orders.length - 1} linked order${orders.length > 2 ? "s" : ""}` : "";
      onPlaced(link === "oco"
        ? `One-cancels-other group placed: ${orders.length} orders (${orders.map((x: any) => x.status.replace("_", " ")).join(", ")})`
        : o.status === "filled"
          ? `Filled: ${o.side} ${fmtQty(o.filled_qty)} ${description} at ${money(o.avg_fill_price, o.avg_fill_price < 1 ? 4 : 2)}${more}`
          : `Order placed: ${o.side} ${fmtQty(o.qty)} ${description} (${o.status.replace("_", " ")})${more}`);
      setPreview(null); setLots(null);
      if (!option) setQty("");
      setLink("none"); setSubs([]); // linked orders are one-off: the next order starts plain
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };

  const sideLabel = option
    ? side === "buy" ? (position && position.qty < 0 ? "Buy to close" : "Buy to open") : position && position.qty > 0 ? "Sell to close" : "Sell to open"
    : side === "buy" ? (position && position.qty < 0 ? "Buy to cover" : "Buy") : position && position.qty > 0 ? "Sell" : "Sell short";

  // Every order the request creates, in the order the server returns them, with the prices entered here.
  const created = preview && preview.ok ? createdOrders(preview, single(), subs, link) : [];

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
      {position && <LotChoice accountId={accountId} symbol={seed.symbol} side={side} position={position} orderQty={qty} value={lots} preview={preview}
        onChange={(v) => edit(() => { setLots(v); if (v) setQty(sumQty(v.map((x) => x.qty))); })} />}
      <label>Order type
        <select value={type} onChange={(e) => edit(() => { setType(e.target.value); if (!["market", "limit"].includes(e.target.value) && (tif === "ioc" || tif === "fok")) setTif("day"); })}>
          {types.map((k) => <option key={k} value={k}>{TYPE_LABELS[k]}</option>)}
        </select>
      </label>
      {auction && (
        <p className="ticket-hint" aria-live="polite">
          {auctionHint || `Waits for the next market ${auctionWhich(type)} and trades once, in that auction.`}
        </p>
      )}
      {hasStop && <label>Stop price<input inputMode="decimal" value={stop} onChange={(e) => edit(() => setStop(e.target.value))} /></label>}
      {hasLimit && <label>Limit price<input inputMode="decimal" value={limit} onChange={(e) => edit(() => setLimit(e.target.value))} /></label>}
      {hasLimit && seed.note && seed.limit_price != null && limit === String(seed.limit_price) && <p className="muted ticket-note">{seed.note}</p>}
      {type === "trailing_stop" && (
        <label>Trail by
          <span className="row">
            <input inputMode="decimal" value={trail} aria-label="Trail by" onChange={(e) => edit(() => setTrail(e.target.value))} />
            <select value={trailMode} aria-label="Trail in" onChange={(e) => edit(() => setTrailMode(e.target.value))}>
              <option value="amount">$</option><option value="percent">%</option>
            </select>
          </span>
        </label>
      )}
      {auction ? (
        <p className="ticket-hint">Time in force: this order lives for one auction only.</p>
      ) : (
        <label>Time in force
          <select value={tif} onChange={(e) => edit(() => setTif(e.target.value))}>
            {Object.entries({ ...TIFS, gtd: "Good till date" }).filter(([k]) => immediateOk || (k !== "ioc" && k !== "fok")).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      )}
      {!auction && tif === "gtd" && (
        <label>Good till date
          <input type="date" value={gtd} min={isoDay(0)} max={isoDay(366)} onChange={(e) => edit(() => setGtd(e.target.value))} />
        </label>
      )}
      {type === "limit" && !option && !linked && (
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
      <LinkedOrders mode={link} subs={subs} oco={subsOco} symbol={seed.symbol} side={side}
        onMode={(m) => edit(() => {
          setLink(m);
          if (m !== "none") { setBracket(false); if (tif === "ioc" || tif === "fok") setTif("day"); }
          const fresh = (n: number) => Array.from({ length: n }, () => newSubOrder(m, seed.symbol, side));
          setSubs(m === "oto" ? fresh(1) : m === "oco" ? fresh(1) : []);
        })}
        onSubs={(s) => edit(() => setSubs(s))} onOco={(v) => edit(() => setSubsOco(v))} />
      {error && <div className="notice bad" role="alert">{error}</div>}
      {preview && preview.ok && (
        <div className="preview">
          <div><span>Estimated price</span><b>{money(preview.estimated_price, preview.estimated_price < 1 ? 4 : 2)}</b></div>
          <div><span>Estimated fees</span><b>{usd(preview.estimated_fees)}</b></div>
          <div><span>{side === "buy" ? "Estimated cost" : "Estimated proceeds"}</span><b>{usd(preview.estimated_total)}</b></div>
          <div><span>Position after</span><b>{fmtQty(preview.position_after)}</b></div>
          {preview.buying_power_after != null && <div><span>Buying power after</span><b>{usd(preview.buying_power_after)}</b></div>}
          {created.length > 1 && (
            <section className="created" aria-label="Orders that will be created">
              <h4>Orders that will be created ({created.length})</h4>
              <ol>
                {created.map((c, i) => (
                  <li key={i}>
                    <span className="created-role">{c.role}</span>
                    <span>{c.text}</span>
                  </li>
                ))}
              </ol>
            </section>
          )}
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

// One line per order: "Buy 10 MKTX, limit 450.00, good till canceled".
function orderLine(side: string, qty: any, description: string, req: any): string {
  const parts = [`${side === "buy" ? "Buy" : "Sell"} ${qty === "" || qty == null ? "the filled quantity of" : fmtQty(Number(qty))} ${description}`];
  const t = TYPE_LABELS[req.type] || req.type;
  const px: string[] = [];
  if (req.stop_price) px.push(`stop ${req.stop_price}`);
  if (req.limit_price) px.push(`limit ${req.limit_price}`);
  if (req.trail_amount) px.push(`trail $${req.trail_amount}`);
  if (req.trail_percent) px.push(`trail ${req.trail_percent}%`);
  parts.push(["limit", "stop", "stop_limit"].includes(req.type) && px.length ? px.join(", ") : [t.toLowerCase(), ...px].join(", "));
  if (req.tif === "gtd" && req.good_till_date) parts.push(`good till ${req.good_till_date}`);
  else if (req.tif === "gtc") parts.push("good till canceled");
  return parts.join(", ");
}

function createdOrders(preview: any, single: any, subs: SubOrder[], link: LinkMode): { role: string; text: string }[] {
  const linked: any[] = preview.linked_orders || [];
  const first = { role: link === "oco" ? "Order 1" : linked.length ? "Entry" : "Order", text: orderLine(preview.side, preview.qty, preview.description, single) };
  const rest = linked.map((o, i) => {
    let req: any = { type: o.type };
    if (link === "none") req = o.role === "take_profit" ? { type: "limit", limit_price: single.take_profit, tif: "gtc" } : { type: "stop", stop_price: single.stop_loss, tif: "gtc" };
    else if (subs[i]) req = subRequest(subs[i]);
    return { role: link === "oco" ? `Order ${i + 2}` : ROLE_LABELS[o.role] || "Linked", text: orderLine(o.side, o.role === "exit" ? "" : o.qty, o.description, req) };
  });
  return [first, ...rest];
}
