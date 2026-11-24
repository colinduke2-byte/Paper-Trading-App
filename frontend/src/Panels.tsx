import { useState } from "react";
import { usePoll } from "./Market";
import { Reports } from "./Reports";
import { api, compact, dateTime, isOption, money, ORDER_TYPES, pct, qty, signed, STATUS, TIFS, tone, usd } from "./util";

const TABS = ["Positions", "Orders", "Fills", "Activity", "Reports", "Alerts", "Screener"];

export function BottomPanel(p: {
  accountId: string; positions: any[]; orders: any[]; refreshKey: number; quotes: Record<string, any>;
  onSelect: (s: string) => void; onClose: (pos: any) => void; onChanged: () => void; notify: (m: string, bad?: boolean) => void;
}) {
  const [tab, setTab] = useState("Positions");
  const open = p.orders.filter((o) => ["working", "partially_filled", "held"].includes(o.status));
  return (
    <section className="panel bottom">
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={t === tab} className={t === tab ? "on" : ""} onClick={() => setTab(t)}>
            {t}{t === "Positions" && p.positions.length ? ` (${p.positions.length})` : ""}{t === "Orders" && open.length ? ` (${open.length})` : ""}
          </button>
        ))}
      </div>
      <div className="scroll">
        {tab === "Positions" && <Positions {...p} />}
        {tab === "Orders" && <Orders {...p} />}
        {tab === "Fills" && <Fills {...p} />}
        {tab === "Activity" && <Activity {...p} />}
        {tab === "Reports" && <Reports accountId={p.accountId} refreshKey={p.refreshKey} notify={p.notify} orders={p.orders} />}
        {tab === "Alerts" && <Alerts {...p} />}
        {tab === "Screener" && <Screener {...p} />}
      </div>
    </section>
  );
}

function Positions({ positions, onSelect, onClose }: any) {
  if (!positions.length) return <p className="muted pad">No open positions. Use the order ticket to place a paper trade.</p>;
  return (
    <table>
      <thead><tr><th>Position</th><th className="num">Qty</th><th className="num">Avg cost</th><th className="num">Mark</th>
        <th className="num">Market value</th><th className="num">Day P&amp;L</th><th className="num">Open P&amp;L</th><th className="num">Open %</th><th /></tr></thead>
      <tbody>
        {positions.map((x: any) => (
          <tr key={x.symbol}>
            <td><button className="link" onClick={() => onSelect(x.underlying || x.symbol)}>{x.description}</button>
              {x.notices?.length ? <span className="tag" title={x.notices.map((n: any) => n.message).join("\n")}> corporate action</span> : null}
              {x.price_note ? <span className="tag" title={x.price_note}>{x.price_source === "model" ? " model price" : " price note"}</span> : null}</td>
            <td className={"num " + tone(x.qty)}>{qty(x.qty)}</td>
            <td className="num">{money(x.avg_cost)}</td>
            <td className="num">{money(x.mark)}</td>
            <td className="num">{usd(x.market_value)}</td>
            <td className={"num " + tone(x.day_pnl)}>{x.day_pnl == null ? "–" : signed(x.day_pnl)}</td>
            <td className={"num " + tone(x.unrealized_pnl)}>{signed(x.unrealized_pnl)}</td>
            <td className={"num " + tone(x.unrealized_pnl)}>{pct(x.unrealized_pct)}</td>
            <td className="num"><button className="ghost sm" onClick={() => onClose(x)}>Close…</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function orderPrice(o: any) {
  if (o.type === "trailing_stop") return `trail ${o.trail_amount != null ? "$" + money(o.trail_amount) : o.trail_percent + "%"}${o.stop_price != null ? ` (stop ${money(o.stop_price)})` : ""}`;
  const parts: string[] = [];
  if (o.stop_price != null) parts.push(`stop ${money(o.stop_price)}`);
  if (o.limit_price != null) parts.push(`limit ${money(o.limit_price)}`);
  return parts.join(", ") || "market";
}

function Orders({ accountId, orders, onChanged, notify, onSelect }: any) {
  const [all, setAll] = useState(false);
  const active = (o: any) => ["working", "partially_filled", "held"].includes(o.status);
  const rows = all ? orders : orders.filter(active);
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
  return (
    <>
      <div className="toolbar">
        <label className="check"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />Show finished orders</label>
        <button className="ghost sm" onClick={cancelAll} disabled={!orders.some(active)}>Cancel all open orders</button>
      </div>
      {rows.length === 0 ? <p className="muted pad">{all ? "No orders yet." : "No open orders."}</p> : (
        <table>
          <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Filled</th><th>Type</th><th>Price</th>
            <th>Duration</th><th className="num">Avg fill</th><th>Status</th><th /></tr></thead>
          <tbody>
            {rows.map((o: any) => (
              <tr key={o.id}>
                <td className="muted">{dateTime(o.created_at)}</td>
                <td><button className="link" onClick={() => onSelect(isOption(o.symbol) ? o.symbol.replace(/\d{6}[CP]\d{8}$/, "") : o.symbol)}>{o.description}</button></td>
                <td className={o.side === "buy" ? "up" : "down"}>{o.side === "buy" ? "Buy" : "Sell"}</td>
                <td className="num">{qty(o.qty)}</td>
                <td className="num">{qty(o.filled_qty)}</td>
                <td>{ORDER_TYPES[o.type]}{o.role ? ` (${o.role.replace("_", " ")})` : ""}</td>
                <td>{orderPrice(o)}</td>
                <td>{TIFS[o.tif]}</td>
                <td className="num">{o.avg_fill_price == null ? "–" : money(o.avg_fill_price, o.avg_fill_price < 1 ? 4 : 2)}</td>
                <td title={o.reason || undefined}>{STATUS[o.status] || o.status}{o.reason && o.status !== "filled" ? <span className="muted"> · {o.reason}</span> : null}</td>
                <td className="num">{active(o) && <button className="ghost sm" onClick={() => cancel(o.id)}>Cancel</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Fills({ accountId, refreshKey }: any) {
  const [rows, error] = usePoll<any[]>(`/accounts/${accountId}/fills`, 0, [refreshKey]);
  if (error) return <div className="notice bad">Could not load fills: {error}</div>;
  if (!rows) return <p className="muted pad">Loading…</p>;
  if (!rows.length) return <p className="muted pad">No fills yet.</p>;
  return (
    <table>
      <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th className="num">Qty</th><th className="num">Price</th><th className="num">Fee</th><th>Kind</th></tr></thead>
      <tbody>
        {rows.map((f) => (
          <tr key={f.id}><td className="muted">{dateTime(f.ts)}</td><td>{f.description}</td>
            <td className={f.side === "buy" ? "up" : "down"}>{f.side === "buy" ? "Buy" : "Sell"}</td>
            <td className="num">{qty(f.qty)}</td><td className="num">{money(f.price, f.price < 1 && f.price > 0 ? 4 : 2)}</td>
            <td className="num">{money(f.fee)}</td><td>{f.kind}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

function Activity({ accountId, refreshKey }: any) {
  const [rows, error] = usePoll<any[]>(`/accounts/${accountId}/ledger`, 0, [refreshKey]);
  if (error) return <div className="notice bad">Could not load activity: {error}</div>;
  if (!rows) return <p className="muted pad">Loading…</p>;
  return (
    <table>
      <thead><tr><th>Time</th><th>Entry</th><th className="num">Cash change</th><th className="num">Realised P&amp;L</th></tr></thead>
      <tbody>
        {rows.map((t) => {
          const get = (name: string) => t.postings.filter((x: any) => x[0] === name).reduce((a: number, x: any) => a + x[1], 0);
          const pnl = -get("income:realized");
          return (
            <tr key={t.id}><td className="muted">{dateTime(t.ts)}</td><td>{t.memo}</td>
              <td className={"num " + tone(get("cash"))}>{signed(get("cash"))}</td>
              <td className={"num " + tone(pnl)}>{pnl ? signed(pnl) : "–"}</td></tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Alerts({ notify, refreshKey }: any) {
  const [alerts, , reload] = usePoll<any[]>(`/alerts`, 5000, [refreshKey]);
  const [symbol, setSymbol] = useState("");
  const [op, setOp] = useState("above");
  const [price, setPrice] = useState("");
  const add = async () => {
    try { await api("/alerts", { method: "POST", body: { symbol, op, price } }); setPrice(""); reload(); }
    catch (e: any) { notify(`Could not add the alert: ${e.message}`, true); }
  };
  return (
    <>
      <div className="toolbar">
        <input value={symbol} placeholder="Symbol" aria-label="Alert symbol" onChange={(e) => setSymbol(e.target.value.toUpperCase())} style={{ width: 90 }} />
        <select value={op} aria-label="Condition" onChange={(e) => setOp(e.target.value)}><option value="above">rises to</option><option value="below">falls to</option></select>
        <input value={price} placeholder="Price" aria-label="Alert price" inputMode="decimal" onChange={(e) => setPrice(e.target.value)} style={{ width: 90 }} />
        <button className="ghost sm" onClick={add} disabled={!symbol || !price}>Add alert</button>
      </div>
      {!alerts || alerts.length === 0 ? <p className="muted pad">No price alerts. Add one above; it shows a notice here when the price is reached.</p> : (
        <table>
          <thead><tr><th>Symbol</th><th>Condition</th><th>Status</th><th /></tr></thead>
          <tbody>
            {alerts.map((a) => (
              <tr key={a.id}><td><b>{a.symbol}</b></td><td>{a.op === "above" ? "Rises to" : "Falls to"} {money(Number(a.price))}</td>
                <td>{a.triggered_at ? `Triggered ${dateTime(a.triggered_at)}` : "Waiting"}</td>
                <td className="num"><button className="ghost sm" onClick={async () => { await api(`/alerts/${a.id}`, { method: "DELETE" }).catch(() => {}); reload(); }}>Remove</button></td></tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Screener({ onSelect }: any) {
  const [f, setF] = useState({ sector: "", min_price: "", max_price: "", min_change_pct: "", max_pe: "", min_dividend_yield: "" });
  const qs = Object.entries(f).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  const [rows, error] = usePoll<any[]>(`/screener?${qs}`, 5000);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const num = (k: string, label: string) => <input key={k} value={(f as any)[k]} placeholder={label} aria-label={label} inputMode="decimal" onChange={set(k)} style={{ width: 110 }} />;
  return (
    <>
      <div className="toolbar">
        <select value={f.sector} aria-label="Sector" onChange={set("sector")}>
          <option value="">All sectors</option>
          {["Technology", "Health Care", "Financials", "Energy", "Consumer", "Industrials", "Utilities", "Communication", "ETF"].map((s) => <option key={s}>{s}</option>)}
        </select>
        {num("min_price", "Min price")}{num("max_price", "Max price")}{num("min_change_pct", "Min change %")}{num("max_pe", "Max P/E")}{num("min_dividend_yield", "Min yield %")}
      </div>
      {error && <div className="notice bad">Screener filters are not valid: {error}</div>}
      {rows && rows.length === 0 && <p className="muted pad">No symbols match these filters.</p>}
      {rows && rows.length > 0 && (
        <table>
          <thead><tr><th>Symbol</th><th>Name</th><th>Sector</th><th className="num">Last</th><th className="num">Chg %</th><th className="num">Volume</th>
            <th className="num">P/E</th><th className="num">Yield</th><th className="num">Mkt cap</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className="click" onClick={() => onSelect(r.symbol)}>
                <td><b>{r.symbol}</b></td><td>{r.name}</td><td className="muted">{r.sector}</td><td className="num">{money(r.last)}</td>
                <td className={"num " + tone(r.change_pct)}>{pct(r.change_pct)}</td><td className="num">{compact(r.volume)}</td>
                <td className="num">{r.pe == null ? "–" : r.pe.toFixed(1)}</td><td className="num">{r.dividend_yield_pct.toFixed(2)}%</td>
                <td className="num">{compact(r.market_cap)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
