// Linked orders in the order ticket: "then place..." follow-on orders (one triggers other) and a
// standalone one-cancels-other group. The ticket's own order is the entry (or the first order of the group).
import { isoDay, TYPE_LABELS, BASIC_TYPES } from "./orderText";

export type LinkMode = "none" | "oto" | "oco";
export type SubOrder = {
  kind: "oto" | "oco"; symbol: string; side: string; qty: string; type: string; limit: string; stop: string;
  trailMode: string; trail: string; tif: string; gtd: string;
};

// The API takes at most five orders in one linked request: an entry and four follow-ons, or five
// orders that cancel each other (backend MAX_LINKED).
export const MAX_FOLLOW_ONS = 4;
export const MAX_OCO = 5;

export function newSubOrder(mode: LinkMode, symbol: string, side: string): SubOrder {
  return { kind: mode === "oco" ? "oco" : "oto", symbol, side: mode === "oto" ? (side === "buy" ? "sell" : "buy") : side,
    qty: "", type: "limit", limit: "", stop: "", trailMode: "amount", trail: "", tif: "gtc", gtd: isoDay(7) };
}

function subBody(s: SubOrder, symbol: string) {
  const hasLimit = s.type === "limit" || s.type === "stop_limit", hasStop = s.type === "stop" || s.type === "stop_limit";
  return {
    symbol: (s.symbol || symbol).toUpperCase().trim(), side: s.side, qty: s.qty === "" ? null : s.qty, type: s.type, tif: s.tif,
    good_till_date: s.tif === "gtd" ? s.gtd : null,
    limit_price: hasLimit ? s.limit : null, stop_price: hasStop ? s.stop : null,
    trail_amount: s.type === "trailing_stop" && s.trailMode === "amount" ? s.trail : null,
    trail_percent: s.type === "trailing_stop" && s.trailMode === "percent" ? s.trail : null,
  };
}

// The request body: one order, an entry with "triggers", or a one-cancels-other group.
export function linkedBody(mode: LinkMode, single: any, subs: SubOrder[], oco: boolean, symbol: string): any {
  if (mode === "oto") return { ...single, take_profit: null, stop_loss: null, triggers: subs.map((s) => subBody(s, symbol)), triggers_oco: oco };
  if (mode === "oco") return { oco: [{ ...single, take_profit: null, stop_loss: null }, ...subs.map((s) => subBody(s, symbol))] };
  return single;
}

export function subRequest(s: SubOrder) { return subBody(s, s.symbol); }

export function LinkedOrders({ mode, subs, oco, symbol, side, onMode, onSubs, onOco }: {
  mode: LinkMode; subs: SubOrder[]; oco: boolean; symbol: string; side: string;
  onMode: (m: LinkMode) => void; onSubs: (s: SubOrder[]) => void; onOco: (v: boolean) => void;
}) {
  const set = (i: number, patch: Partial<SubOrder>) => onSubs(subs.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const max = mode === "oto" ? MAX_FOLLOW_ONS : MAX_OCO - 1;
  return (
    <div className="linked">
      <label>Linked orders
        <select value={mode} onChange={(e) => onMode(e.target.value as LinkMode)}>
          <option value="none">None</option>
          <option value="oto">Then place more orders when this one fills</option>
          <option value="oco">One cancels the other</option>
        </select>
      </label>
      {mode === "oto" && <p className="ticket-hint">One triggers other (OTO): the orders below wait, on hold, until this order fills completely. Leave the quantity blank for an exit that closes what this order bought or sold.</p>}
      {mode === "oco" && <p className="ticket-hint">One cancels the other (OCO): this order and the ones below all work at once; when one fills, the others are reduced or canceled. Two to five orders, whole quantities.</p>}
      {mode !== "none" && subs.map((s, i) => {
        const name = mode === "oto" ? `Follow-on order ${i + 1}` : `Order ${i + 2}`;
        const hasLimit = s.type === "limit" || s.type === "stop_limit", hasStop = s.type === "stop" || s.type === "stop_limit";
        return (
          <fieldset key={i} className="sub-order">
            <legend>{name}</legend>
            <div className="sub-grid">
              <label>Symbol<input value={s.symbol} autoCapitalize="characters" autoCorrect="off" spellCheck={false}
                onChange={(e) => set(i, { symbol: e.target.value.toUpperCase() })} /></label>
              <label>Side
                <select value={s.side} onChange={(e) => set(i, { side: e.target.value })}>
                  <option value="buy">Buy</option><option value="sell">Sell</option>
                </select>
              </label>
              <label>Quantity<input inputMode="decimal" value={s.qty} placeholder={mode === "oto" ? "Same as filled" : "0"}
                onChange={(e) => set(i, { qty: e.target.value })} /></label>
              <label>Order type
                <select value={s.type} onChange={(e) => set(i, { type: e.target.value })}>
                  {BASIC_TYPES.map((k) => <option key={k} value={k}>{TYPE_LABELS[k]}</option>)}
                </select>
              </label>
              {hasStop && <label>Stop price<input inputMode="decimal" value={s.stop} onChange={(e) => set(i, { stop: e.target.value })} /></label>}
              {hasLimit && <label>Limit price<input inputMode="decimal" value={s.limit} onChange={(e) => set(i, { limit: e.target.value })} /></label>}
              {s.type === "trailing_stop" && (
                <label>Trail by
                  <span className="row">
                    <input inputMode="decimal" value={s.trail} aria-label="Trail by" onChange={(e) => set(i, { trail: e.target.value })} />
                    <select value={s.trailMode} aria-label="Trail in" onChange={(e) => set(i, { trailMode: e.target.value })}>
                      <option value="amount">$</option><option value="percent">%</option>
                    </select>
                  </span>
                </label>
              )}
              <label>Time in force
                <select value={s.tif} onChange={(e) => set(i, { tif: e.target.value })}>
                  {mode === "oco" && <option value="day">Day</option>}
                  <option value="gtc">Good till canceled</option>
                  <option value="gtd">Good till date</option>
                </select>
              </label>
              {s.tif === "gtd" && <label>Good till date<input type="date" value={s.gtd} min={isoDay(0)} max={isoDay(366)} onChange={(e) => set(i, { gtd: e.target.value })} /></label>}
            </div>
            {(mode === "oto" || subs.length > 1) && (
              <button className="ghost sm" onClick={() => onSubs(subs.filter((_, j) => j !== i))} aria-label={`Remove ${name.toLowerCase()}`}>Remove</button>
            )}
          </fieldset>
        );
      })}
      {mode !== "none" && (
        <button className="ghost sm" disabled={subs.length >= max}
          onClick={() => onSubs([...subs, newSubOrder(mode, symbol, side)])}>
          {subs.length >= max ? `At most ${mode === "oto" ? `${MAX_FOLLOW_ONS} follow-on orders` : `${MAX_OCO} orders in a group`}` : mode === "oto" ? "Add a follow-on order" : "Add an order to the group"}
        </button>
      )}
      {mode === "oto" && subs.length > 1 && (
        <label className="check"><input type="checkbox" checked={oco} onChange={(e) => onOco(e.target.checked)} />Cancel the others when one fills</label>
      )}
    </div>
  );
}
