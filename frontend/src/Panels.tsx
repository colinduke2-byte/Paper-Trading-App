import { useState } from "react";
import { AlertsTab } from "./AlertsTab";
import { ChangeOrderButton } from "./ChangeOrder";
import { usePoll } from "./Market";
import { Reports } from "./Reports";
import { Lots } from "./Lots";
import { OrdersTable, PositionsTable } from "./OrdersView";
import { api, compact, dateTime, money, numText, pct, price, qty, signed, tone } from "./util";

const TABS = ["Positions", "Lots", "Orders", "Fills", "Activity", "Reports", "Alerts", "Screener"];

export function BottomPanel(p: {
  accountId: string; positions: any[]; orders: any[]; refreshKey: number; quotes: Record<string, any>;
  onSelect: (s: string) => void; onClose: (pos: any) => void; onChanged: () => void; notify: (m: string, bad?: boolean) => void;
  onCloseStrategy?: (rows: any[], name: string) => void; // opens the strategy builder with the opposite legs
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
        {tab === "Positions" && <PositionsTable {...p} />}
        {tab === "Lots" && <Lots accountId={p.accountId} refreshKey={p.refreshKey} onSell={p.onClose} />}
        {tab === "Orders" && <OrdersTable {...p} />}
        {tab === "Fills" && <Fills {...p} />}
        {tab === "Activity" && <Activity {...p} />}
        {tab === "Reports" && <Reports accountId={p.accountId} refreshKey={p.refreshKey} notify={p.notify} orders={p.orders} />}
        {tab === "Alerts" && <AlertsTab {...p} />}
        {tab === "Screener" && <Screener {...p} />}
      </div>
    </section>
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
            <td className="num">{qty(f.qty)}</td><td className="num">{price(f.price)}</td>
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

function Screener({ onSelect }: any) {
  const [f, setF] = useState({ asset_class: "", sector: "", min_price: "", max_price: "", min_change_pct: "", max_pe: "", min_dividend_yield: "" });
  const qs = Object.entries(f).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${encodeURIComponent(k === "sector" || k === "asset_class" ? v : numText(v))}`).join("&");
  const [rows, error] = usePoll<any[]>(`/screener?${qs}`, 5000);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const num = (k: string, label: string) => <input key={k} value={(f as any)[k]} placeholder={label} aria-label={label} inputMode="decimal" onChange={set(k)} style={{ width: 110 }} />;
  return (
    <>
      <div className="toolbar">
        <select value={f.asset_class} aria-label="Asset class" onChange={set("asset_class")}>
          <option value="">All asset classes</option><option value="stock">Stocks and funds</option><option value="crypto">Crypto</option>
        </select>
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
                <td><b>{r.symbol}</b></td><td>{r.name}</td><td className="muted">{r.sector}</td><td className="num">{price(r.last)}</td>
                <td className={"num " + tone(r.change_pct)}>{pct(r.change_pct)}</td><td className="num">{compact(r.volume)}</td>
                <td className="num">{r.pe == null ? "–" : r.pe.toFixed(1)}</td><td className="num">{r.dividend_yield_pct.toFixed(2)}%</td>
                <td className="num">{r.asset_class === "crypto" ? "–" : compact(r.market_cap)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
