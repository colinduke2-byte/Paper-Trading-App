import { useEffect, useRef, useState } from "react";
import { ColorType, LineSeries, createChart } from "lightweight-charts";
import { usePoll } from "./Market";
import { api, dateTime, money, qty, signed, tone, usd } from "./util";
import "./reports.css";
import { TaxYear, WashDetail } from "./TaxYear";

const VIEWS = ["Overview", "Gains", "Tax year", "Statements", "Journal", "Export"] as const;
type View = (typeof VIEWS)[number];

const LOT_LABELS: Record<string, string> = {
  fifo: "First in, first out", lifo: "Last in, first out", highest_cost: "Highest cost first",
  lowest_cost: "Lowest cost first", tax_loss: "Tax-efficient: largest loss first",
};
const METHOD_SHORT: Record<string, string> = { ...LOT_LABELS, specific: "Chosen lot" };
const EXPORTS: [string, string][] = [
  ["fills", "Fills"], ["orders", "Orders"], ["ledger", "Ledger"], ["positions", "Positions"], ["gains", "Realised gains"], ["journal", "Journal"],
];

const isNum = (v: any): v is number => typeof v === "number" && isFinite(v);
const ratioPct = (v: any, dp = 2) => (isNum(v) ? (v > 0 ? "+" : "") + (v * 100).toFixed(dp) + "%" : "–");
const plainPct = (v: any, dp = 1) => (isNum(v) ? (v * 100).toFixed(dp) + "%" : "–");
const fixed = (v: any, dp = 2) => (isNum(v) ? v.toFixed(dp) : "–");
const day = (ts: any) => (isNum(ts) ? new Date(ts * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "–");

function duration(sec: any) {
  if (!isNum(sec)) return "–";
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 86400 * 2) return `${(sec / 3600).toFixed(1)} h`;
  return `${(sec / 86400).toFixed(1)} days`;
}

export function Reports({ accountId, refreshKey, notify, orders }: { accountId: string; refreshKey: number; notify: (m: string, bad?: boolean) => void; orders: any[] }) {
  const [view, setView] = useState<View>("Overview");
  return (
    <div className="reports">
      <div className="toolbar reports-nav">
        <div className="seg" role="group" aria-label="Report">
          {VIEWS.map((v) => <button key={v} className={v === view ? "on" : ""} aria-pressed={v === view} onClick={() => setView(v)}>{v}</button>)}
        </div>
        <span className="muted rep-label">All figures use simulated prices.</span>
      </div>
      {view === "Overview" && <Overview accountId={accountId} refreshKey={refreshKey} />}
      {view === "Gains" && <Gains accountId={accountId} refreshKey={refreshKey} notify={notify} />}
      {view === "Tax year" && <TaxYear accountId={accountId} refreshKey={refreshKey} />}
      {view === "Statements" && <Statements accountId={accountId} refreshKey={refreshKey} />}
      {view === "Journal" && <Journal accountId={accountId} refreshKey={refreshKey} notify={notify} orders={orders} />}
      {view === "Export" && <Exports accountId={accountId} />}
    </div>
  );
}

// ---- overview ----------------------------------------------------------------------------------
function Overview({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const [bench, setBench] = useState("");
  const [benchInput, setBenchInput] = useState("");
  const [rf, setRf] = useState("");
  const qs = new URLSearchParams();
  if (bench) qs.set("benchmark", bench);
  if (rf.trim() && isFinite(Number(rf))) qs.set("risk_free", String(Number(rf) / 100));
  const [data, error] = usePoll<any>(`/accounts/${accountId}/analytics?${qs}`, 15000, [refreshKey]);
  const controls = (
      <div className="toolbar">
        <label className="inline">Benchmark
          <input value={benchInput} placeholder="MKTX" aria-label="Benchmark symbol" style={{ width: 90 }} autoCapitalize="characters"
            onChange={(e) => setBenchInput(e.target.value.toUpperCase())} onKeyDown={(e) => e.key === "Enter" && setBench(benchInput.trim())}
            onBlur={() => setBench(benchInput.trim())} />
        </label>
        <label className="inline">Risk-free rate %
          <input value={rf} placeholder="0" inputMode="decimal" aria-label="Risk-free rate, percent per year" style={{ width: 70 }} onChange={(e) => setRf(e.target.value)} />
        </label>
      </div>
  );
  if (error) return <div className="rep-body">{controls}<div className="notice bad" role="alert">Could not load the account statistics: {error}</div></div>;
  if (!data) return <div className="rep-body">{controls}<p className="muted pad">Loading statistics…</p></div>;
  const r = data.returns, b = data.benchmark, t = data.trades.overall;
  const stat = (k: string, v: string, cls = "", title?: string) => <div key={k} title={title}><span>{k}</span><b className={cls}>{v}</b></div>;
  const dd = r.max_drawdown ? `${day(r.drawdown_peak_at)} to ${day(r.drawdown_trough_at)}${r.drawdown_recovered_at ? `, recovered ${day(r.drawdown_recovered_at)}` : ", not yet recovered"}` : "No drawdown yet";
  return (
    <div className="rep-body">
      {controls}
      <h4 className="rep-h">Returns and risk</h4>
      <div className="facts">
        {stat("Time-weighted return", ratioPct(r.time_weighted_return), tone(r.time_weighted_return), "Return with deposits and withdrawals taken out")}
        {b && stat(`${b.symbol} return`, ratioPct(b.total_return), tone(b.total_return), b.name)}
        {b && stat("Versus benchmark", ratioPct(b.excess_return), tone(b.excess_return))}
        {stat("Volatility (annual)", plainPct(r.volatility), "", "Needs at least two days of history")}
        {stat("Sharpe ratio", fixed(r.sharpe), "", "Needs at least two days of history")}
        {stat("Max drawdown", plainPct(r.max_drawdown, 2), r.max_drawdown ? "down" : "", dd)}
        {b && stat("Beta", fixed(b.beta))}
        {stat("Days measured", String(r.trading_days))}
      </div>
      {r.max_drawdown > 0 && <p className="muted rep-note">Largest drawdown: {dd}.</p>}
      <ValueChart account={data.account_curve} bench={b?.curve} benchName={b?.symbol} />
      <h4 className="rep-h">Closed trades</h4>
      {t.trades === 0 ? <p className="muted pad">No closed trades yet. Statistics appear once a position is closed.</p> : (
        <>
          <div className="facts">
            {stat("Closed lots", String(t.trades))}
            {stat("Win rate", plainPct(t.win_rate, 0))}
            {stat("Average win", usd(t.average_win), "up")}
            {stat("Average loss", usd(t.average_loss), "down")}
            {stat("Profit factor", fixed(t.profit_factor))}
            {stat("Expectancy", signed(t.expectancy), tone(t.expectancy), "Average gain per closed lot")}
            {stat("Largest win", usd(t.largest_win))}
            {stat("Largest loss", usd(t.largest_loss))}
            {stat("Average holding time", duration(t.average_holding_seconds))}
          </div>
          <Breakdown title="By side" rows={data.trades.by_direction} label={(k) => (k === "long" ? "Long" : "Short")} />
          <Breakdown title="By instrument" rows={data.trades.by_asset} label={(k) => (k === "stock" ? "Stocks" : "Options")} />
          <Breakdown title="By symbol" rows={data.trades.by_symbol} label={(k) => k} />
        </>
      )}
      <h4 className="rep-h">Exposure</h4>
      {data.exposure.by_symbol.length === 0 ? <p className="muted pad">No open positions.</p> : (
        <>
          <div className="facts">
            {stat("Long", usd(data.exposure.totals.long))}
            {stat("Short", usd(data.exposure.totals.short))}
            {stat("Gross leverage", plainPct(data.exposure.totals.gross_leverage))}
            {stat("Net leverage", plainPct(data.exposure.totals.net_leverage))}
          </div>
          <div className="rep-table">
            <table>
              <thead><tr><th>Sector</th><th className="num">Long</th><th className="num">Short</th><th className="num">Net</th><th className="num">Net % of value</th></tr></thead>
              <tbody>
                {data.exposure.by_sector.map((s: any) => (
                  <tr key={s.sector}><td>{s.sector}</td><td className="num">{usd(s.long)}</td><td className="num">{usd(s.short)}</td>
                    <td className="num">{usd(s.net)}</td><td className="num">{plainPct(s.net_weight)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted rep-note">{data.exposure.note}</p>
        </>
      )}
    </div>
  );
}

function Breakdown({ title, rows, label }: { title: string; rows: Record<string, any>; label: (k: string) => string }) {
  const keys = Object.keys(rows);
  if (keys.length < 2 && title !== "By symbol") return null;
  return (
    <div className="rep-table">
      <table>
        <thead><tr><th>{title}</th><th className="num">Lots</th><th className="num">Win rate</th><th className="num">Avg win</th><th className="num">Avg loss</th>
          <th className="num">Profit factor</th><th className="num">Total</th></tr></thead>
        <tbody>
          {keys.map((k) => {
            const s = rows[k];
            return (
              <tr key={k}><td>{label(k)}</td><td className="num">{s.trades}</td><td className="num">{plainPct(s.win_rate, 0)}</td>
                <td className="num">{usd(s.average_win)}</td><td className="num">{usd(s.average_loss)}</td><td className="num">{fixed(s.profit_factor)}</td>
                <td className={"num " + tone(s.total_gain)}>{signed(s.total_gain)}</td></tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function toLine(points: number[][] | undefined, shift: number) {
  const out: { time: any; value: number }[] = [];
  for (const [t, v] of points || []) {
    const s = Math.floor(t) + shift;
    if (out.length && out[out.length - 1].time >= s) out[out.length - 1] = { time: out[out.length - 1].time, value: v };
    else out.push({ time: s, value: v });
  }
  return out;
}

function ValueChart({ account, bench, benchName }: { account: number[][]; bench?: number[][]; benchName?: string }) {
  const el = useRef<HTMLDivElement>(null);
  const ref = useRef<any>(null);
  const shift = -new Date().getTimezoneOffset() * 60; // show local time on the axis
  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "#12161c" }, textColor: "#8591a1", fontSize: 11 },
      grid: { vertLines: { color: "#1d232c" }, horzLines: { color: "#1d232c" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#1d232c" },
      rightPriceScale: { borderColor: "#1d232c" },
      handleScroll: { vertTouchDrag: false },
    });
    const acct = chart.addSeries(LineSeries, { color: "#4c9aff", lineWidth: 2, priceLineVisible: false, title: "Account" });
    const bm = chart.addSeries(LineSeries, { color: "#e0a13a", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    ref.current = { chart, acct, bm };
    return () => { chart.remove(); ref.current = null; };
  }, []);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    c.acct.setData(toLine(account, shift));
    c.bm.setData(toLine(bench, shift));
    c.bm.applyOptions({ title: benchName ? `${benchName} (scaled)` : "" });
    c.chart.timeScale().fitContent();
  }, [JSON.stringify(account?.[account.length - 1]), account?.length, bench?.length, benchName]);
  const enough = account && account.length > 1;
  return (
    <div className="rep-chart-wrap">
      <div className="rep-legend">
        <span><i className="sw acct" />Account value</span>
        {bench && <span><i className="sw bench" />{benchName} scaled to the starting value</span>}
      </div>
      <div className="rep-chart" ref={el} aria-label="Account value over time against the benchmark" role="img" />
      {!enough && <p className="muted rep-note">The curve fills in as the account is sampled, about once a minute.</p>}
    </div>
  );
}

// ---- gains -------------------------------------------------------------------------------------
function Gains({ accountId, refreshKey, notify }: { accountId: string; refreshKey: number; notify: (m: string, bad?: boolean) => void }) {
  const [data, error, reload] = usePoll<any>(`/accounts/${accountId}/gains`, 0, [refreshKey]);
  const [journal, , reloadJ] = usePoll<any[]>(`/accounts/${accountId}/journal?target_type=trade`, 0, [refreshKey]);
  const [noting, setNoting] = useState<string | null>(null);
  const setMethod = async (method: string) => {
    try {
      await api(`/accounts/${accountId}/lot-method`, { method: "PUT", body: { method } });
      notify(`Tax-lot method set to ${LOT_LABELS[method].toLowerCase()}. It applies to trades closed from now on.`);
      reload();
    } catch (e: any) { notify(`Could not change the tax-lot method: ${e.message}`, true); }
  };
  if (error) return <div className="notice bad" role="alert">Could not load realised gains: {error}</div>;
  if (!data) return <p className="muted pad">Loading realised gains…</p>;
  const notes: Record<string, any[]> = {};
  for (const j of journal || []) (notes[j.target_id] ||= []).push(j);
  const tt = data.totals;
  return (
    <div className="rep-body">
      <div className="toolbar">
        <label className="inline">Tax-lot method
          <select value={data.lot_method} onChange={(e) => setMethod(e.target.value)} aria-label="Tax-lot method">
            {Object.entries(LOT_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <a className="ghost sm btnlink" href={`/api/accounts/${accountId}/export/gains.csv`} download>Download CSV</a>
      </div>
      <div className="facts">
        <div><span>Realised gain</span><b className={tone(tt.gain)}>{signed(tt.gain)}</b></div>
        <div><span>Short-term</span><b className={tone(tt.short_term)}>{signed(tt.short_term)}</b></div>
        <div><span>Long-term</span><b className={tone(tt.long_term)}>{signed(tt.long_term)}</b></div>
        <div><span>Proceeds</span><b>{usd(tt.proceeds)}</b></div>
        <div><span>Cost</span><b>{usd(tt.cost)}</b></div>
        <div><span>Wash-sale flags</span><b className={tt.wash_sale_lots ? "warn" : ""}>{tt.wash_sale_lots}</b></div>
        <div><span>Wash-sale disallowed (informational)</span><b className={tt.wash_sale_disallowed ? "warn" : ""}>{usd(tt.wash_sale_disallowed)}</b></div>
      </div>
      {data.rows.length === 0 ? <p className="muted pad">No closed lots yet. Each lot appears here when a trade closes it.</p> : (
        <div className="rep-table">
          <table>
            <thead><tr><th>Lot</th><th>Tax lot</th><th>Method</th><th>Instrument</th><th>Side</th><th className="num">Qty</th><th>Opened</th><th>Closed</th><th className="num">Proceeds</th>
              <th className="num">Cost</th><th className="num">Gain</th><th>Term</th><th>Wash sale</th><th>Journal</th></tr></thead>
            <tbody>
              {[...data.rows].reverse().map((r: any) => (
                <tr key={r.id}>
                  <td className="muted">{r.id}</td><td>{r.lot_id || "–"}</td>
                  <td title={r.lot_fallback ? "The chosen lot was no longer open when this filled, so the account's method chose" : undefined}>
                    {METHOD_SHORT[r.lot_method] || r.lot_method}{r.lot_fallback ? " (fallback)" : ""}</td><td>{r.description}</td><td>{r.direction === "long" ? "Long" : "Short"}</td>
                  <td className="num">{qty(r.qty)}</td><td>{r.open_date}</td><td>{r.close_date}</td>
                  <td className="num">{money(r.proceeds)}</td><td className="num">{money(r.cost)}</td>
                  <td className={"num " + tone(r.gain)}>{signed(r.gain)}</td><td>{r.term === "long" ? "Long-term" : "Short-term"}</td>
                  <td><WashDetail row={r} /></td>
                  <td><button className="ghost sm" onClick={() => setNoting(noting === r.id ? null : r.id)}>
                    {notes[r.id]?.length ? `Notes (${notes[r.id].length})` : "Add note"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {noting && (
        <div className="rep-card">
          <b>Journal for lot {noting}</b>
          {(notes[noting] || []).map((j) => <Entry key={j.id} accountId={accountId} entry={j} notify={notify} onChanged={reloadJ} />)}
          <EntryForm accountId={accountId} targetType="trade" targetId={noting} notify={notify} onSaved={reloadJ} />
        </div>
      )}
      <ul className="rep-notes muted">{data.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>
    </div>
  );
}

// ---- statements --------------------------------------------------------------------------------
function Statements({ accountId, refreshKey }: { accountId: string; refreshKey: number }) {
  const [list, listError] = usePoll<any>(`/accounts/${accountId}/statements`, 0, [refreshKey]);
  const [month, setMonth] = useState("");
  const current = month || list?.months?.[0] || "";
  const [st, error] = usePoll<any>(current ? `/accounts/${accountId}/statements/${current}` : null, 0, [refreshKey]);
  if (listError) return <div className="notice bad" role="alert">Could not load the list of statements: {listError}</div>;
  if (!list) return <p className="muted pad">Loading statements…</p>;
  return (
    <div className="rep-body">
      <div className="toolbar">
        <label className="inline">Month
          <select value={current} onChange={(e) => setMonth(e.target.value)} aria-label="Statement month">
            {list.months.map((m: string) => <option key={m} value={m}>{monthName(m)}</option>)}
          </select>
        </label>
        {current && <a className="ghost sm btnlink" href={`/api/accounts/${accountId}/statements/${current}/html`} target="_blank" rel="noopener">Open printable statement</a>}
      </div>
      {error && <div className="notice bad" role="alert">Could not load the statement: {error}</div>}
      {!st && !error && <p className="muted pad">Loading statement…</p>}
      {st && st.month === current && (
        <>
          {st.current && <p className="muted rep-note">This month is still in progress; closing figures are live.</p>}
          <div className="rep-table">
            <table className="rep-summary">
              <tbody>
                <tr><td>Opening account value</td><td className="num">{usd(st.opening.account_value)}</td></tr>
                <tr><td>Deposits and withdrawals</td><td className="num">{signed(st.deposits_total)}</td></tr>
                <tr><td>Realised profit and loss</td><td className={"num " + tone(st.realized_pnl)}>{signed(st.realized_pnl)}</td></tr>
                <tr><td>Fees</td><td className="num">{signed(-st.fees)}</td></tr>
                <tr><td>Other income and charges{Object.keys(st.other_by_kind).length ? ` (${Object.keys(st.other_by_kind).join(", ")})` : ""}</td><td className="num">{signed(st.other_total)}</td></tr>
                <tr><td>Change in unrealised profit and loss</td><td className={"num " + tone(st.unrealized_change)}>{signed(st.unrealized_change)}</td></tr>
                <tr className="total"><td>Closing account value</td><td className="num">{usd(st.closing.account_value)}</td></tr>
              </tbody>
            </table>
          </div>
          <div className="facts">
            <div><span>Opening cash</span><b>{usd(st.opening.cash)}</b></div>
            <div><span>Closing cash</span><b>{usd(st.closing.cash)}</b></div>
            <div><span>Trades</span><b>{st.trade_count}</b></div>
            <div><span>Bought</span><b>{usd(st.bought)}</b></div>
            <div><span>Sold</span><b>{usd(st.sold)}</b></div>
          </div>
          <h4 className="rep-h">Positions at month end</h4>
          {st.positions.length === 0 ? <p className="muted pad">No positions at the end of this month.</p> : (
            <div className="rep-table">
              <table>
                <thead><tr><th>Instrument</th><th className="num">Qty</th><th className="num">Cost basis</th><th className="num">Mark</th><th className="num">Market value</th><th className="num">Unrealised</th></tr></thead>
                <tbody>
                  {st.positions.map((p: any) => (
                    <tr key={p.symbol}><td>{p.description}</td><td className="num">{qty(p.qty)}</td><td className="num">{usd(p.cost_basis)}</td>
                      <td className="num">{money(p.mark)}</td><td className="num">{usd(p.market_value)}</td><td className={"num " + tone(p.unrealized_pnl)}>{signed(p.unrealized_pnl)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <ul className="rep-notes muted">{st.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>
        </>
      )}
    </div>
  );
}

function monthName(m: string) {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleDateString([], { year: "numeric", month: "long" });
}

// ---- journal -----------------------------------------------------------------------------------
function Journal({ accountId, refreshKey, notify, orders }: { accountId: string; refreshKey: number; notify: (m: string, bad?: boolean) => void; orders: any[] }) {
  const [tag, setTag] = useState("");
  const [entries, error, reload] = usePoll<any[]>(`/accounts/${accountId}/journal${tag ? `?tag=${encodeURIComponent(tag)}` : ""}`, 0, [refreshKey]);
  const [gains] = usePoll<any>(`/accounts/${accountId}/gains`, 0, [refreshKey]);
  const [targetType, setTargetType] = useState<"trade" | "order">("trade");
  const [targetId, setTargetId] = useState("");
  const trades = gains ? [...gains.rows].reverse() : [];
  const choices = targetType === "trade"
    ? trades.map((r: any) => [r.id, `${r.id} · ${r.description} · ${signed(r.gain)} · ${r.close_date}`])
    : orders.map((o: any) => [o.id, `${o.id} · ${o.side === "buy" ? "Buy" : "Sell"} ${qty(o.qty)} ${o.description} · ${o.status.replace("_", " ")}`]);
  const chosen = choices.some(([id]) => id === targetId) ? targetId : choices[0]?.[0] || "";
  if (error) return <div className="notice bad" role="alert">Could not load the journal: {error}</div>;
  const allTags = Array.from(new Set((entries || []).flatMap((e) => e.tags))).sort();
  return (
    <div className="rep-body">
      <div className="rep-card">
        <b>New journal entry</b>
        <div className="rep-form-row">
          <label className="inline">Attach to
            <select value={targetType} onChange={(e) => { setTargetType(e.target.value as any); setTargetId(""); }} aria-label="Attach to">
              <option value="trade">Closed trade</option><option value="order">Order</option>
            </select>
          </label>
          <select value={chosen} onChange={(e) => setTargetId(e.target.value)} aria-label={targetType === "trade" ? "Closed trade" : "Order"} className="grow" disabled={!choices.length}>
            {!choices.length && <option value="">{targetType === "trade" ? "No closed trades yet" : "No orders yet"}</option>}
            {choices.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </div>
        {chosen && <EntryForm key={targetType + chosen} accountId={accountId} targetType={targetType} targetId={chosen} notify={notify} onSaved={reload} />}
      </div>
      <div className="toolbar">
        <label className="inline">Filter by tag
          <select value={tag} onChange={(e) => setTag(e.target.value)} aria-label="Filter by tag">
            <option value="">All entries</option>
            {(tag && !allTags.includes(tag) ? [tag, ...allTags] : allTags).map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <a className="ghost sm btnlink" href={`/api/accounts/${accountId}/export/journal.csv`} download>Download CSV</a>
      </div>
      {!entries ? <p className="muted pad">Loading journal…</p> : entries.length === 0 ? (
        <p className="muted pad">{tag ? "No entries with this tag." : "No journal entries yet. Add a note to a closed trade or an order above."}</p>
      ) : entries.map((j) => <Entry key={j.id} accountId={accountId} entry={j} notify={notify} onChanged={reload} showTarget />)}
    </div>
  );
}

function EntryForm({ accountId, targetType, targetId, notify, onSaved, entry, onDone }: {
  accountId: string; targetType?: string; targetId?: string; notify: (m: string, bad?: boolean) => void; onSaved: () => void; entry?: any; onDone?: () => void;
}) {
  const [note, setNote] = useState(entry?.note || "");
  const [tags, setTags] = useState((entry?.tags || []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true); setError("");
    try {
      if (entry) await api(`/accounts/${accountId}/journal/${entry.id}`, { method: "PATCH", body: { note, tags } });
      else await api(`/accounts/${accountId}/journal`, { method: "POST", body: { target_type: targetType, target_id: targetId, note, tags } });
      if (!entry) { setNote(""); setTags(""); }
      notify(entry ? "Journal entry saved" : "Journal entry added");
      onSaved(); onDone?.();
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };
  return (
    <div className="rep-form">
      <label>Note<textarea value={note} rows={3} maxLength={4000} onChange={(e) => setNote(e.target.value)} placeholder="Why you took the trade, what happened, what to repeat or avoid" /></label>
      <label>Tags<input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="Comma-separated, for example breakout, earnings" autoCapitalize="none" /></label>
      {error && <div className="notice bad" role="alert">Could not save the entry: {error}</div>}
      <div className="rep-form-row">
        {onDone && <button className="ghost" onClick={onDone}>Cancel</button>}
        <button className="primary" onClick={save} disabled={busy || (!note.trim() && !tags.trim())}>{entry ? "Save changes" : "Add entry"}</button>
      </div>
    </div>
  );
}

function Entry({ accountId, entry, notify, onChanged, showTarget }: { accountId: string; entry: any; notify: (m: string, bad?: boolean) => void; onChanged: () => void; showTarget?: boolean }) {
  const [editing, setEditing] = useState(false);
  const remove = async () => {
    if (!window.confirm("Delete this journal entry? This cannot be undone.")) return;
    try { await api(`/accounts/${accountId}/journal/${entry.id}`, { method: "DELETE" }); notify("Journal entry deleted"); onChanged(); }
    catch (e: any) { notify(`Could not delete the entry: ${e.message}`, true); }
  };
  if (editing) return <div className="rep-entry"><EntryForm accountId={accountId} entry={entry} notify={notify} onSaved={onChanged} onDone={() => setEditing(false)} /></div>;
  return (
    <div className="rep-entry">
      <div className="rep-entry-head">
        {showTarget && <b>{entry.target_type === "trade" ? "Closed trade" : "Order"} {entry.target_id}</b>}
        <span className="muted">{dateTime(entry.updated_at)}{entry.updated_at !== entry.created_at ? " (edited)" : ""}</span>
        <span className="grow" />
        <button className="ghost sm" onClick={() => setEditing(true)}>Edit</button>
        <button className="ghost sm" onClick={remove}>Delete</button>
      </div>
      {entry.note && <p className="rep-entry-note">{entry.note}</p>}
      {entry.tags.length > 0 && <div className="rep-tags">{entry.tags.map((t: string) => <span key={t} className="chip">{t}</span>)}</div>}
    </div>
  );
}

// ---- export ------------------------------------------------------------------------------------
function Exports({ accountId }: { accountId: string }) {
  return (
    <div className="rep-body">
      <p className="muted rep-note">Each file is comma-separated text with times in UTC (ISO 8601) and exact amounts, ready for a spreadsheet.</p>
      <div className="rep-downloads">
        {EXPORTS.map(([k, label]) => (
          <a key={k} className="ghost btnlink" href={`/api/accounts/${accountId}/export/${k}.csv`} download>{label} (CSV)</a>
        ))}
      </div>
    </div>
  );
}
