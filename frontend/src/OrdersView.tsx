// The Positions and Orders tabs of the bottom panel: option strategies grouped under a heading,
// multi-leg orders as one row that opens to show its legs, and linked orders shown together.
import { Fragment, useState } from "react";
import { api, dateTime, isOption, money, pct, price, qty, signed, STATUS, tone, usd } from "./util";
import { ChangeOrderButton } from "./ChangeOrder";
import { describeLeg, Leg, legFromSymbol, recognise } from "./payoff";
import { durationText, netText, ROLE_LABELS, TYPE_LABELS } from "./orderText";
import "./orders.css";

// ---- positions ---------------------------------------------------------------------------------
type PosGroup = { key: string; name: string; underlying: string; rows: any[] };

const posLeg = (p: any): Leg => legFromSymbol(p.symbol, p.qty > 0 ? "buy" : "sell", Math.abs(p.qty));

// Option positions on one underlying that together form a common strategy (with the shares, for a
// covered call or protective put). Display only: the positions themselves are unchanged.
function combos<T>(xs: T[], k: number): T[][] {
  if (k === 0) return [[]];
  return xs.flatMap((x, i) => combos(xs.slice(i + 1), k - 1).map((rest) => [x, ...rest]));
}

export function groupPositions(positions: any[]): PosGroup[] {
  const groups: PosGroup[] = [];
  const unders = [...new Set(positions.filter((p) => isOption(p.symbol)).map((p) => p.underlying))];
  for (const u of unders) {
    let left = positions.filter((p) => isOption(p.symbol) && p.underlying === u);
    const stock = positions.find((p) => p.symbol === u && p.qty > 0);
    const take = (rows: any[], name: string) => {
      groups.push({ key: `${u}:${rows.map((r) => r.symbol).join(",")}`, name, underlying: u, rows });
      left = left.filter((p) => !rows.includes(p));
    };
    // Largest recognisable sets first (an iron condor before the spreads inside it); a position
    // whose size is shared by two strategies stays ungrouped rather than being split.
    if (left.length <= 10) {
      for (const k of [4, 3, 2]) {
        for (let found = true; found;) {
          found = false;
          for (const rows of combos(left, k)) {
            const name = recognise(rows.map(posLeg));
            if (name) { take(rows, name); found = true; break; }
          }
        }
      }
    }
    if (stock && left.length) {
      for (const k of [2, 1]) {
        const hit = combos(left, k).map((rows) => [stock, ...rows]).find((rows) => recognise(rows.map(posLeg)));
        if (hit) { take(hit, recognise(hit.map(posLeg))!); break; }
      }
    }
  }
  return groups;
}

export function PositionsTable({ positions, onSelect, onClose, onCloseStrategy }: {
  positions: any[]; onSelect: (s: string) => void; onClose: (p: any) => void; onCloseStrategy?: (rows: any[], name: string) => void;
}) {
  const [grouped, setGrouped] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  if (!positions.length) return <p className="muted pad">No open positions. Use the order ticket to place a paper trade.</p>;
  const groups = groupPositions(positions);
  const inGroup = new Set(grouped ? groups.flatMap((g) => g.rows.map((r) => r.symbol)) : []);
  const row = (x: any, cls = "") => (
    <tr key={x.symbol} className={cls}>
      <td><button className="link" onClick={() => onSelect(x.underlying || x.symbol)}>{x.description}</button>
        {x.asset_class === "crypto" && <span className="tag" title="Crypto: fully paid, trades 24/7">Crypto</span>}</td>
      <td className={"num " + tone(x.qty)}>{qty(x.qty)}</td>
      <td className="num">{price(x.avg_cost)}</td>
      <td className="num" title={x.price_unavailable ? "No price has arrived for this position yet" : undefined}>{x.mark == null ? "–" : price(x.mark)}</td>
      <td className="num">{x.market_value == null ? "–" : usd(x.market_value)}</td>
      <td className={"num " + tone(x.day_pnl)}>{x.day_pnl == null ? "–" : signed(x.day_pnl)}</td>
      <td className={"num " + tone(x.unrealized_pnl)}>{x.unrealized_pnl == null ? "–" : signed(x.unrealized_pnl)}</td>
      <td className={"num " + tone(x.unrealized_pnl)}>{x.unrealized_pct == null ? "–" : pct(x.unrealized_pct)}</td>
      <td className="num"><button className="ghost sm" onClick={() => onClose(x)}>Close…</button></td>
    </tr>
  );
  return (
    <>
      {groups.length > 0 && (
        <div className="toolbar">
          <label className="check"><input type="checkbox" checked={grouped} onChange={(e) => setGrouped(e.target.checked)} />Group option strategies</label>
        </div>
      )}
      <table>
        <thead><tr><th>Position</th><th className="num">Qty</th><th className="num">Avg cost</th><th className="num">Mark</th>
          <th className="num">Market value</th><th className="num">Day P&amp;L</th><th className="num">Open P&amp;L</th><th className="num">Open %</th><th /></tr></thead>
        <tbody>
          {grouped && groups.map((g) => {
            const open = !collapsed[g.key];
            const value = g.rows.reduce((a, r) => a + (r.market_value ?? 0), 0);
            const pnl = g.rows.reduce((a, r) => a + (r.unrealized_pnl ?? 0), 0);
            return (
              <Fragment key={g.key}>
                <tr className="pos-group" data-strategy={g.name}>
                  <td colSpan={9}>
                    <div className="pg-head">
                      <button className="pg-toggle" aria-expanded={open} onClick={() => setCollapsed({ ...collapsed, [g.key]: open })}>
                        <span className="caret" aria-hidden="true">{open ? "▾" : "▸"}</span>{g.name} · {g.underlying}
                      </button>
                      <span className="muted">{g.rows.length} legs</span>
                      <span>Net value <b>{usd(value)}</b></span>
                      <span>Open P&amp;L <b className={tone(pnl)}>{signed(pnl)}</b></span>
                      {onCloseStrategy && <button className="ghost sm" onClick={() => onCloseStrategy(g.rows, g.name)}>Close strategy…</button>}
                    </div>
                  </td>
                </tr>
                {open && g.rows.map((r) => row(r, "pos-leg"))}
              </Fragment>
            );
          })}
          {positions.filter((p) => !inGroup.has(p.symbol)).map((p) => row(p))}
        </tbody>
      </table>
    </>
  );
}

// ---- orders ------------------------------------------------------------------------------------
const ACTIVE = ["working", "partially_filled", "held"];
const active = (o: any) => ACTIVE.includes(o.status);

function orderPrice(o: any) {
  if (o.type === "multileg") return o.limit_price == null ? "market" : `net ${netText(o.limit_price)}`;
  if (o.type === "trailing_stop") return `trail ${o.trail_amount != null ? "$" + money(o.trail_amount) : o.trail_percent + "%"}${o.stop_price != null ? ` (stop ${money(o.stop_price)})` : ""}`;
  const parts: string[] = [];
  if (o.stop_price != null) parts.push(`stop ${price(o.stop_price)}`);
  if (o.limit_price != null) parts.push(`limit ${price(o.limit_price)}`);
  return parts.join(", ") || (o.type.endsWith("_close") ? "closing price" : o.type.endsWith("_open") ? "opening price" : "market");
}

const legsOf = (o: any): Leg[] => (o.legs || []).map((l: any) => legFromSymbol(l.symbol, l.side, l.ratio));
export const strategyName = (o: any) => recognise(legsOf(o)) || `Multi-leg, ${o.legs.length} legs`;

type OrderGroup = { key: string; kind: string | null; orders: any[] };

// Bracket, one-triggers-other and one-cancels-other orders are kept together, entry first.
function groupOrders(orders: any[]): OrderGroup[] {
  const map = new Map<string, any[]>();
  for (const o of orders) {
    const key = o.parent_id || o.oco_group || o.id;
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(o);
  }
  return [...map.entries()].map(([key, list]) => {
    const sorted = [...list].sort((a, b) => (a.id < b.id ? -1 : 1));
    let kind: string | null = null;
    if (sorted.length > 1) {
      const kids = sorted.filter((o) => o.parent_id);
      if (!kids.length) kind = "One cancels the other";
      else if (kids.some((o) => o.role === "take_profit" || o.role === "stop_loss")) kind = "Bracket order";
      else kind = `One triggers other${kids.length > 1 && kids.every((k) => k.oco_group) ? ", follow-on orders cancel each other" : ""}`;
    }
    return { key, kind, orders: sorted };
  });
}

function roleOf(o: any, g: OrderGroup): string | null {
  if (!g.kind) return o.role ? ROLE_LABELS[o.role] || o.role : null;
  if (g.kind === "One cancels the other") return `Order ${g.orders.indexOf(o) + 1} of ${g.orders.length}`;
  if (!o.parent_id) return "Entry";
  return ROLE_LABELS[o.role] || o.role;
}

export function OrdersTable({ accountId, orders, onChanged, notify, onSelect }: {
  accountId: string; orders: any[]; onChanged: () => void; notify: (m: string, bad?: boolean) => void; onSelect: (s: string) => void;
}) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const groups = groupOrders(orders).filter((g) => all || g.orders.some(active));
  const cancel = async (id: string) => {
    try { await api(`/accounts/${accountId}/orders/${id}`, { method: "DELETE" }); notify("Order canceled"); }
    catch (e: any) { notify(`Could not cancel the order: ${e.message}`, true); }
    onChanged();
  };
  const cancelAll = async () => {
    try { const r = await api(`/accounts/${accountId}/orders`, { method: "DELETE" }); notify(`${r.canceled} order${r.canceled === 1 ? "" : "s"} canceled`); }
    catch (e: any) { notify(`Could not cancel orders: ${e.message}`, true); }
    onChanged();
  };
  const row = (o: any, g: OrderGroup) => {
    const role = roleOf(o, g);
    const multi = o.type === "multileg";
    const expanded = !!open[o.id];
    const units = (n: number) => qty(n);
    return (
      <Fragment key={o.id}>
        <tr className={(g.kind ? "og-member" : "") + (multi ? " ml-row" : "")} data-order={o.id}>
          <td className="muted">{dateTime(o.created_at)}</td>
          <td>
            {role && <span className="role-tag">{role}</span>}
            {multi ? (
              <button className="link ml-toggle" aria-expanded={expanded} onClick={() => setOpen({ ...open, [o.id]: !expanded })}>
                <span className="caret" aria-hidden="true">{expanded ? "▾" : "▸"}</span>{strategyName(o)} · {o.symbol}
              </button>
            ) : (
              <button className="link" onClick={() => onSelect(isOption(o.symbol) ? o.symbol.replace(/\d{6}[CP]\d{8}$/, "") : o.symbol)}>{o.description}</button>
            )}
          </td>
          <td className={multi ? "" : o.side === "buy" ? "up" : "down"}>{multi ? (o.limit_price == null ? "–" : o.limit_price < 0 ? "Net credit" : "Net debit") : o.side === "buy" ? "Buy" : "Sell"}</td>
          <td className="num">{units(o.qty)}</td>
          <td className="num">{units(o.filled_qty)}</td>
          <td>{TYPE_LABELS[o.type] || o.type}</td>
          <td>{orderPrice(o)}</td>
          <td>{durationText(o)}</td>
          <td className="num">{o.avg_fill_price == null ? "–" : multi ? netText(o.avg_fill_price) : price(o.avg_fill_price)}</td>
          <td title={o.reason || undefined}>{STATUS[o.status] || o.status}
            {o.status === "held" && o.parent_id ? <span className="muted"> · waits for the entry</span> : null}
            {o.reason && o.status !== "filled" ? <span className="muted"> · {o.reason}</span> : null}</td>
          <td className="num row-actions">{active(o) && <>
            {!multi && <ChangeOrderButton accountId={accountId} order={o} onChanged={onChanged} notify={notify} />}
            <button className="ghost sm" onClick={() => cancel(o.id)}>Cancel</button></>}</td>
        </tr>
        {multi && expanded && o.legs.map((l: any, i: number) => {
          const leg = legFromSymbol(l.symbol, l.side, l.ratio);
          return (
            <tr key={o.id + i} className="ml-leg">
              <td className="muted">Leg {i + 1}</td>
              <td>{describeLeg(o.symbol, leg)}</td>
              <td className={l.side === "buy" ? "up" : "down"}>{l.side === "buy" ? "Buy" : "Sell"}</td>
              <td className="num">{units(l.ratio * o.qty)}</td>
              <td className="num">{units(l.ratio * o.filled_qty)}</td>
              <td className="muted">ratio {l.ratio}</td>
              <td colSpan={5} />
            </tr>
          );
        })}
      </Fragment>
    );
  };
  return (
    <>
      <div className="toolbar">
        <label className="check"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />Show finished orders</label>
        <button className="ghost sm" onClick={cancelAll} disabled={!orders.some(active)}>Cancel all open orders</button>
      </div>
      {groups.length === 0 ? <p className="muted pad">{all ? "No orders yet." : "No open orders."}</p> : (
        <table>
          <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Filled</th><th>Type</th><th>Price</th>
            <th>Duration</th><th className="num">Avg fill</th><th>Status</th><th /></tr></thead>
          <tbody>
            {groups.map((g) => (
              <Fragment key={g.key}>
                {g.kind && (
                  <tr className="og-head" data-group={g.kind}>
                    <td colSpan={11}><span className="og-label">{g.kind} · {g.orders.length} orders</span></td>
                  </tr>
                )}
                {g.orders.map((o) => row(o, g))}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
