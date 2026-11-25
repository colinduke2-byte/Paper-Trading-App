import { useState } from "react";
import { Dialog } from "./Dialog";
import { api, money, numText, qty as fmtQty } from "./util";

// "Change…" on a working single-leg order: new quantity and price, sent as PATCH /orders/{id} (the server
// re-runs every check and answers with the reason when it refuses). Self-contained so the Orders table can
// place it wherever its row layout puts actions.
const CHANGEABLE = ["working", "partially_filled", "held"];

export const canChange = (o: any) => CHANGEABLE.includes(o.status) && !(o.legs && o.legs.length);

export function ChangeOrderButton({ accountId, order, onChanged, notify }: {
  accountId: string; order: any; onChanged: () => void; notify: (m: string, bad?: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!canChange(order)) return null;
  return (
    <>
      <button className="ghost sm" aria-label={`Change order ${order.description}`} onClick={() => setOpen(true)}>Change…</button>
      {open && <ChangeOrderDialog accountId={accountId} order={order} onClose={() => setOpen(false)}
        onDone={(msg) => { setOpen(false); notify(msg); onChanged(); }} />}
    </>
  );
}

function ChangeOrderDialog({ accountId, order: o, onClose, onDone }: {
  accountId: string; order: any; onClose: () => void; onDone: (msg: string) => void;
}) {
  const hasLimit = o.limit_price != null;
  const hasStop = o.stop_price != null && (o.type === "stop" || o.type === "stop_limit");
  const [q, setQ] = useState(String(o.qty));
  const [limit, setLimit] = useState(hasLimit ? String(o.limit_price) : "");
  const [stop, setStop] = useState(hasStop ? String(o.stop_price) : "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const oco = !!o.oco_group;

  const submit = async () => {
    setError("");
    const body: Record<string, string> = {};
    const nq = numText(q), nl = numText(limit), ns = numText(stop);
    if (nq !== String(o.qty)) body.qty = nq;
    if (hasLimit && nl !== String(o.limit_price)) body.limit_price = nl;
    if (hasStop && ns !== String(o.stop_price)) body.stop_price = ns;
    if (!Object.keys(body).length) { setError("Nothing was changed."); return; }
    if (oco && body.qty && Number(body.qty) > o.qty) {
      setError("The quantity of a one-cancels-other order cannot be raised; lower it or cancel and enter a new order.");
      return;
    }
    setBusy(true);
    try {
      const r = await api(`/accounts/${accountId}/orders/${o.id}`, { method: "PATCH", body });
      const price = [r.stop_price != null && hasStop ? `stop ${money(r.stop_price)}` : "", r.limit_price != null ? `limit ${money(r.limit_price)}` : ""]
        .filter(Boolean).join(", ");
      onDone(`Order changed: ${r.side} ${fmtQty(r.qty)} ${r.description}${price ? `, ${price}` : ""}`);
    } catch (e: any) {
      setError(`The order was not changed: ${e.message}`);
      setBusy(false);
    }
  };

  return (
    <Dialog onClose={onClose} labelledBy="change-order-title" className="modal change-order">
      <h3 id="change-order-title">Change order: {o.side === "buy" ? "Buy" : "Sell"} {o.description}</h3>
      {o.filled_qty > 0 && <p className="muted">{fmtQty(o.filled_qty)} already filled. The quantity is the order's total, filled part included.</p>}
      {oco && <p className="muted">This order is one-cancels-other: its quantity can be lowered but not raised.</p>}
      <label>Quantity<input value={q} inputMode="decimal" autoFocus onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && submit()} /></label>
      {hasStop && <label>Stop price<input value={stop} inputMode="decimal" onChange={(e) => setStop(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && submit()} /></label>}
      {hasLimit && <label>Limit price<input value={limit} inputMode="decimal" onChange={(e) => setLimit(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && submit()} /></label>}
      {error && <div className="notice bad" role="alert">{error}</div>}
      <div className="row">
        <button className="ghost" onClick={onClose}>Keep as is</button>
        <button className="primary" disabled={busy} onClick={submit}>{busy ? "Changing…" : "Change order"}</button>
      </div>
    </Dialog>
  );
}
