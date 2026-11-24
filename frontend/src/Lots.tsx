import { useEffect, useState } from "react";
import { usePoll } from "./Market";
import { api, money, qty, signed, tone, usd } from "./util";
import "./lots.css";

// Tax lots. Two pieces:
// - Lots: the "Lots" tab of the bottom panel. Open lots per position; tick lots and press "Sell selected
//   lots" to open the order ticket prefilled with those lots (App's onClose with a `lots` field).
// - LotChoice: the optional, collapsed "Choose tax lots" section of the order ticket. It sends
//   lots: [{ lot_id, qty }] with the order; without it the account's lot method chooses.

export type LotChoiceItem = { lot_id: string; qty: number };

const METHODS: Record<string, string> = {
  fifo: "first in, first out", lifo: "last in, first out", highest_cost: "highest cost first",
  lowest_cost: "lowest cost first", tax_loss: "tax-efficient, largest loss first",
};
const method = (m: string) => METHODS[m] || m;
const SOURCE: Record<string, string> = { model: "model price", synthetic: "simulated price" };
const sourceLabel = (s: string) => SOURCE[s] || `${s} quote`;

function holding(l: any) {
  if (l.direction === "short") return "Short-term (short sale)";
  if (l.term === "long") return "Long-term";
  return `Short-term · long-term on ${l.long_term_on}`;
}

const unit = (p: any) => (p.kind === "option" ? "contracts" : "shares");

function LotFacts({ l, p }: { l: any; p: any }) {
  return (
    <div className="lot-facts">
      <div><span>Cost per unit</span><b>{money(l.cost_per_unit, l.cost_per_unit < 1 ? 4 : 2)}</b></div>
      <div><span>Cost basis</span><b>{usd(l.cost_basis)}</b></div>
      <div><span>Value</span><b title={`At ${money(p.mark)}, ${sourceLabel(p.source)}`}>{usd(l.market_value)}</b></div>
      <div><span>Unrealised</span><b className={tone(l.unrealized_pnl)}>{signed(l.unrealized_pnl)}</b></div>
      <div className="wide"><span>Holding period</span><b>{holding(l)}</b></div>
      {l.wash_sale && (
        <div className="wide lot-wash">
          <span>Wash-sale adjustment (informational)</span>
          <b>{usd(l.wash_sale.disallowed_loss_added)} disallowed loss added: basis {usd(l.wash_sale.adjusted_cost_basis)}, holding from {l.wash_sale.adjusted_holding_start}</b>
        </div>
      )}
    </div>
  );
}

export function Lots({ accountId, refreshKey, onSell }: { accountId: string; refreshKey: number; onSell: (pos: any) => void }) {
  const [data, error] = usePoll<any>(`/accounts/${accountId}/lots`, 10000, [refreshKey]);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [symbol, setSymbol] = useState<string | null>(null);
  if (error) return <div className="notice bad" role="alert">Could not load tax lots: {error}</div>;
  if (!data) return <p className="muted pad">Loading tax lots…</p>;
  if (!data.positions.length) return <p className="muted pad">No open lots. Each purchase (or short sale) opens a lot.</p>;
  const toggle = (sym: string, id: string) => {
    const next = symbol === sym ? { ...picked } : {};
    next[id] = !next[id];
    setSymbol(sym);
    setPicked(next);
  };
  const sell = (p: any) => {
    const chosen = p.lots.filter((l: any) => picked[l.id] && l.available > 0);
    const lots: LotChoiceItem[] = chosen.map((l: any) => ({ lot_id: l.id, qty: l.available }));
    const total = lots.reduce((a, x) => a + x.qty, 0);
    onSell({ symbol: p.symbol, underlying: p.kind === "option" ? p.underlying : undefined, qty: p.qty > 0 ? total : -total, lots });
  };
  return (
    <div className="lots" aria-label="Tax lots">
      <p className="muted lots-note">Lot method for trades that name no lots: <b>{method(data.lot_method)}</b>. {data.notes[0]}</p>
      {data.positions.map((p: any) => {
        const n = symbol === p.symbol ? p.lots.filter((l: any) => picked[l.id] && l.available > 0).length : 0;
        const verb = p.qty > 0 ? "Sell" : "Buy to cover";
        return (
          <section key={p.symbol} className="lot-pos" aria-label={`Lots of ${p.description}`}>
            <header>
              <b>{p.description}</b>
              <span className="muted">{qty(p.qty)} {unit(p)} · mark {money(p.mark)} ({sourceLabel(p.source)})</span>
              <button className={p.qty > 0 ? "primary sell sm" : "primary buy sm"} disabled={!n} onClick={() => sell(p)}>
                {verb} selected lots{n ? ` (${n})` : ""}
              </button>
            </header>
            {p.lots.map((l: any) => (
              <div key={l.id} className={"lot-row" + (picked[l.id] && symbol === p.symbol ? " on" : "")}>
                <label className="lot-pick">
                  <input type="checkbox" checked={!!picked[l.id] && symbol === p.symbol} disabled={l.available <= 0}
                    aria-label={`Select lot ${l.id}`} onChange={() => toggle(p.symbol, l.id)} />
                  <span><b>{l.id}</b> · {qty(Math.abs(l.qty))} {unit(p)} {l.direction === "short" ? "short " : ""}opened {l.open_date}
                    {l.opened_by !== "trade" ? ` by ${l.opened_by}` : ""}
                    {l.reserved > 0 && <span className="muted"> · {qty(l.reserved)} named by an open order</span>}</span>
                </label>
                <LotFacts l={l} p={p} />
              </div>
            ))}
          </section>
        );
      })}
    </div>
  );
}

// ---- the order ticket's optional lot section ------------------------------------------------------
export function LotChoice({ accountId, symbol, side, position, orderQty, value, onChange, preview }: {
  accountId: string; symbol: string; side: string; position: any; orderQty: string; value: LotChoiceItem[] | null;
  onChange: (v: LotChoiceItem[] | null) => void; preview: any;
}) {
  const [data, setData] = useState<any>(null);
  const [open, setOpen] = useState(!!value?.length);
  useEffect(() => { if (value?.length) setOpen(true); }, [value?.length]);
  const closes = !!position && ((position.qty > 0 && side === "sell") || (position.qty < 0 && side === "buy"));
  // The lots are loaded only while the section is open.
  useEffect(() => {
    if (!open || !closes) return;
    let live = true;
    api(`/accounts/${accountId}/lots?symbol=${encodeURIComponent(symbol)}`).then((d) => live && setData(d)).catch(() => live && setData(null));
    return () => { live = false; };
  }, [accountId, symbol, open, closes]);
  // A choice only makes sense for an order that closes the position: drop it when the side flips.
  useEffect(() => { if (!closes && value) onChange(null); }, [closes]);
  if (!closes) return null;
  const pos = data?.positions?.[0];
  const chosen = value || [];
  const get = (id: string) => chosen.find((c) => c.lot_id === id);
  const set = (id: string, q: number | null) => {
    const rest = chosen.filter((c) => c.lot_id !== id);
    const next = q == null ? rest : [...rest, { lot_id: id, qty: q }];
    const order = (pos?.lots || []).map((l: any) => l.id);
    next.sort((a, b) => order.indexOf(a.lot_id) - order.indexOf(b.lot_id));
    onChange(next.length ? next : null);
  };
  const total = chosen.reduce((a, c) => a + (Number(c.qty) || 0), 0);
  const mismatch = chosen.length > 0 && Number(orderQty) !== total;
  const est: Record<string, any> = {};
  for (const r of preview?.lots || []) est[r.lot_id] = r;
  return (
    <details className="lot-choice" open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>Choose tax lots (optional){chosen.length ? ` · ${chosen.length} chosen` : ""}</summary>
      {!pos && <p className="muted">Loading lots…</p>}
      {pos && <p className="muted">Without a choice, lots close by the account's method ({method(data.lot_method)}).</p>}
      {pos?.lots.map((l: any) => {
        const c = get(l.id);
        return (
          <div key={l.id} className={"lot-row" + (c ? " on" : "")}>
            <label className="lot-pick">
              <input type="checkbox" checked={!!c} disabled={l.available <= 0} aria-label={`Close lot ${l.id}`}
                onChange={(e) => set(l.id, e.target.checked ? l.available : null)} />
              <span><b>{l.id}</b> · {qty(Math.abs(l.qty))} @ {money(l.cost_per_unit, l.cost_per_unit < 1 ? 4 : 2)} · {l.open_date} · {l.term === "long" ? "long-term" : "short-term"}
                <span className={tone(l.unrealized_pnl)}> {signed(l.unrealized_pnl)}</span>
                {l.available < Math.abs(l.qty) && <span className="muted"> · {qty(l.available)} available</span>}</span>
            </label>
            {c && (
              <label className="lot-qty">Quantity from {l.id}
                <input inputMode="decimal" value={String(c.qty)} aria-label={`Quantity from lot ${l.id}`}
                  onChange={(e) => set(l.id, e.target.value === "" ? 0 : Number(e.target.value))} />
              </label>
            )}
            {c && est[l.id] && <span className="muted">Estimated gain {signed(est[l.id].estimated_gain)} ({est[l.id].term}-term)</span>}
          </div>
        );
      })}
      {mismatch && <div className="notice warn">The chosen lots add up to {qty(total)}; the order is for {orderQty || 0}. Make them match.</div>}
      {chosen.length > 0 && <button className="ghost sm" onClick={() => onChange(null)}>Use the account's method instead</button>}
    </details>
  );
}
