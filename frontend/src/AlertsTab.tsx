import { useEffect, useMemo, useState } from "react";
import { usePoll } from "./Market";
import { api, dateTime, isOption, money } from "./util";
import "./alerts.css";

// Alerts: the builder dialog (every alert type) and the Alerts tab (list, edit, pause, delete).
//
// "Alert me at this price" from the chart (or anywhere else): call requestPriceAlert(symbol, price).
// App.tsx listens for the event and opens the builder with a price alert filled in, so the caller
// needs no props or state of its own.
export const ALERT_AT_PRICE = "pd:alert-at-price";
export function requestPriceAlert(symbol: string, price: number) {
  window.dispatchEvent(new CustomEvent(ALERT_AT_PRICE, { detail: { symbol, price } }));
}

export type AlertSeed = { kind?: string; symbol?: string; price?: number | string; op?: string; account_id?: string };

const KINDS: [string, string][] = [
  ["price", "Price"], ["change", "Percent change"], ["volume", "Volume"], ["indicator", "Indicator"],
  ["option", "Option contract"], ["position", "Position gain or loss"], ["account", "Account value and margin"],
  ["reminder", "Reminder"], ["calendar", "Dividend, split and earnings dates"],
];
const KIND_LABEL = Object.fromEntries(KINDS);
const ACCOUNT_ONLY = ["position", "account", "calendar"];
const TFS: [string, string][] = [["1m", "1 minute"], ["5m", "5 minutes"], ["15m", "15 minutes"], ["1h", "1 hour"], ["1d", "1 day"], ["1w", "1 week"]];
const ACCOUNT_METRICS: [string, string][] = [["equity", "Account value"], ["buying_power", "Buying power"],
  ["margin_excess", "Margin excess (value above the maintenance requirement)"], ["unrealized_pnl", "Unrealised gain/loss, all positions"],
  ["day_pnl", "Day gain/loss"]];

type Form = Record<string, any>;

function initialForm(seed: AlertSeed | undefined, editing: any, accountId: string): Form {
  if (editing) {
    const p = editing.params || {};
    return {
      kind: editing.kind, symbol: editing.symbol || "", op: String(editing.op || "above").includes("below") ? "below" : "above",
      value: editing.kind === "price" ? editing.price : editing.value, scope: editing.account_id ? "account" : "all",
      basis: p.basis || "day", reference: p.reference || "", mode: p.mode || "level", indicator: p.indicator || "sma",
      tf: p.tf || "1d", period: String(p.period ?? 20), metric: p.metric || (editing.kind === "option" ? "iv" : editing.kind === "position" ? "unrealized_pnl" : "equity"),
      minutes: "", events: p.events || ["dividend", "split"], days_before: String(p.days_before ?? 3),
      repeat: !!editing.repeat, cooldown: String(Math.round((editing.cooldown || 900) / 60)),
      expires: "", message: editing.message || "", keepExpiry: editing.expires_at || null,
    };
  }
  return {
    kind: seed?.kind || "price", symbol: seed?.symbol || "", op: seed?.op || "above",
    value: seed?.price != null ? String(seed.price) : "", scope: accountId ? "account" : "all",
    basis: "day", reference: "", mode: "level", indicator: "sma", tf: "1d", period: "20", metric: "",
    minutes: "60", events: ["dividend", "split"], days_before: "3", repeat: false, cooldown: "15", expires: "", message: "",
    keepExpiry: null,
  };
}

function toRequest(f: Form, accountId: string): any {
  const kind = f.kind;
  const scoped = ACCOUNT_ONLY.includes(kind) || f.scope === "account";
  const req: any = { kind, account_id: scoped ? accountId : null, repeat: f.repeat, message: f.message.trim() || null };
  if (f.repeat) req.cooldown = Math.round(Number(f.cooldown || 0) * 60);
  // Hours of market time: the server adds them to its clock (the replay clock during a replay).
  if (f.expires) req.expires_in_hours = f.expires;
  else req.expires_at = f.keepExpiry || null;
  const sym = String(f.symbol || "").trim().toUpperCase();
  if (kind === "price") Object.assign(req, { symbol: sym, op: f.op, price: f.value });
  if (kind === "change") Object.assign(req, { symbol: sym, op: f.op, value: f.value, basis: f.basis, reference: f.basis === "reference" ? f.reference || null : null });
  if (kind === "volume") Object.assign(req, { symbol: sym, value: f.value, mode: f.mode });
  if (kind === "indicator") {
    Object.assign(req, { symbol: sym, indicator: f.indicator, tf: f.tf, period: Number(f.period) });
    const below = f.op === "below";
    req.op = f.indicator === "rsi" ? (below ? "below" : "above") : below ? "crosses_below" : "crosses_above";
    req.value = f.indicator === "rsi" ? f.value : "0";
  }
  if (kind === "option") Object.assign(req, { symbol: sym, op: f.op, value: f.value, metric: f.metric || "iv" });
  if (kind === "position") Object.assign(req, { symbol: sym, op: f.op, value: f.value, metric: f.metric || "unrealized_pnl" });
  if (kind === "account") Object.assign(req, { op: f.op, value: f.value, metric: f.metric || "equity" });
  if (kind === "reminder") { if (f.minutes) req.in_minutes = f.minutes; }
  if (kind === "calendar") Object.assign(req, { events: f.events, days_before: Number(f.days_before) });
  return req;
}

export function AlertBuilder({ accountId, seed, editing, quotes, positions, onClose, onSaved }: {
  accountId: string; seed?: AlertSeed; editing?: any; quotes: Record<string, any>; positions: any[];
  onClose: () => void; onSaved: (msg: string) => void;
}) {
  const [f, setF] = useState<Form>(() => initialForm(seed, editing, accountId));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: any) => setF((x) => ({ ...x, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const [last, setLast] = useState<number | null>(null);
  const sym = String(f.symbol || "").trim().toUpperCase();
  useEffect(() => {
    const q = quotes[sym];
    if (q) { setLast(q.last); return; }
    if (!sym || !/^[A-Z0-9.]{1,21}$/.test(sym)) { setLast(null); return; }
    let alive = true;
    const t = setTimeout(() => api(`/quotes?symbols=${encodeURIComponent(sym)}`).then((r) => alive && setLast(r[sym]?.last ?? null)).catch(() => {}), 250);
    return () => { alive = false; clearTimeout(t); };
  }, [sym, quotes[sym]?.last]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  const options = positions.filter((p) => isOption(p.symbol));
  const kind = f.kind;
  const cond = (labels: [string, string] = ["rises to", "falls to"]) => (
    <label>Condition
      <select value={f.op === "below" ? "below" : "above"} onChange={set("op")}>
        <option value="above">{labels[0]}</option><option value="below">{labels[1]}</option>
      </select>
    </label>
  );
  const symbolField = (label = "Symbol") => (
    <label>{label}<input value={f.symbol} onChange={(e) => setF({ ...f, symbol: e.target.value.toUpperCase() })} placeholder="e.g. MKTX"
      autoCapitalize="characters" autoComplete="off" spellCheck={false} />
      {last != null && <span className="hint">Last price {money(last)}</span>}
    </label>
  );
  const save = async () => {
    setBusy(true); setError("");
    try {
      const req = toRequest(f, accountId);
      const r = editing
        ? await api(`/alert-rules/${editing.id}`, { method: "PATCH", body: { ...req, kind: undefined } })
        : await api("/alert-rules", { method: "POST", body: req });
      onSaved(editing ? `Alert updated: ${r.description}` : `Alert created: ${r.description}`);
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };
  const isCross = kind === "indicator" && f.indicator !== "rsi";
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal alert-builder" role="dialog" aria-modal="true" aria-label={editing ? "Edit alert" : "New alert"} onClick={(e) => e.stopPropagation()}>
        <h3>{editing ? "Edit alert" : "New alert"}</h3>
        <label>Alert type
          <select value={kind} disabled={!!editing} onChange={(e) => setF({ ...f, kind: e.target.value, op: "above", value: e.target.value === kind ? f.value : "", metric: "" })}>
            {KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </label>

        {kind === "price" && <>{symbolField()}{cond()}<label>Price<input value={f.value} inputMode="decimal" onChange={set("value")} /></label></>}

        {kind === "change" && <>
          {symbolField()}
          <label>Measured from
            <select value={f.basis} onChange={set("basis")}><option value="day">The previous close (change on the day)</option><option value="reference">A reference price</option></select>
          </label>
          {f.basis === "reference" && <label>Reference price<input value={f.reference} inputMode="decimal" placeholder={last != null ? `current price ${money(last)}` : ""} onChange={set("reference")} /></label>}
          {cond()}
          <label>Percent change<input value={f.value} inputMode="decimal" placeholder="e.g. 5 or -3" onChange={set("value")} /></label>
        </>}

        {kind === "volume" && <>
          {symbolField()}
          <label>Compare today's volume with
            <select value={f.mode} onChange={set("mode")}><option value="level">A number of shares</option><option value="average">A multiple of the 30-day average</option></select>
          </label>
          <label>{f.mode === "level" ? "Shares" : "Times the average"}<input value={f.value} inputMode="decimal" placeholder={f.mode === "level" ? "e.g. 1000000" : "e.g. 2"} onChange={set("value")} /></label>
        </>}

        {kind === "indicator" && <>
          {symbolField()}
          <label>Indicator
            <select value={f.indicator} onChange={(e) => setF({ ...f, indicator: e.target.value, period: e.target.value === "rsi" ? "14" : f.period === "14" ? "20" : f.period })}>
              <option value="sma">Price crosses its simple moving average</option>
              <option value="ema">Price crosses its exponential moving average</option>
              <option value="rsi">Relative strength index (RSI)</option>
              <option value="macd">MACD (12, 26, 9) crosses its signal line</option>
            </select>
          </label>
          <div className="row2">
            <label>Bars<select value={f.tf} aria-label="Bars" onChange={set("tf")}>{TFS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
            {f.indicator !== "macd" && <label>Period (bars)<input value={f.period} inputMode="numeric" onChange={set("period")} /></label>}
          </div>
          {isCross ? cond(["crosses above", "crosses below"]) : cond(["rises above", "falls below"])}
          {f.indicator === "rsi" && <label>RSI level<input value={f.value} inputMode="decimal" placeholder="e.g. 70" onChange={set("value")} /></label>}
          <p className="hint">Checked when each bar closes, using the closing price.</p>
        </>}

        {kind === "option" && <>
          <label>Contract
            {options.length > 0 && (
              <select value={options.some((p) => p.symbol === f.symbol) ? f.symbol : ""} onChange={set("symbol")} aria-label="Contract you hold">
                <option value="">Type a contract symbol below, or pick one you hold</option>
                {options.map((p) => <option key={p.symbol} value={p.symbol}>{p.description}</option>)}
              </select>
            )}
            <input value={f.symbol} onChange={(e) => setF({ ...f, symbol: e.target.value.toUpperCase() })} placeholder="e.g. MKTX261218C00100000"
              aria-label="Contract symbol" autoCapitalize="characters" autoComplete="off" spellCheck={false} />
            <span className="hint">The contract symbol is shown in the option chain and the order ticket.</span>
          </label>
          <label>Measure
            <select value={f.metric || "iv"} onChange={set("metric")}><option value="iv">Implied volatility (%)</option><option value="delta">Delta</option></select>
          </label>
          {cond()}
          <label>{(f.metric || "iv") === "iv" ? "Implied volatility (%)" : "Delta (-1 to 1)"}<input value={f.value} inputMode="decimal" onChange={set("value")} /></label>
        </>}

        {kind === "position" && <>
          <label>Position
            <select value={f.symbol} onChange={set("symbol")}>
              <option value="">{positions.length ? "Choose a position" : "No open positions"}</option>
              {positions.map((p) => <option key={p.symbol} value={p.symbol}>{p.description}</option>)}
            </select>
          </label>
          <label>Measure
            <select value={f.metric || "unrealized_pnl"} onChange={set("metric")}><option value="unrealized_pnl">Unrealised gain/loss ($)</option><option value="unrealized_pct">Unrealised gain/loss (%)</option></select>
          </label>
          {cond()}
          <label>Value<input value={f.value} inputMode="decimal" placeholder="e.g. -500" onChange={set("value")} /></label>
        </>}

        {kind === "account" && <>
          <label>Measure
            <select value={f.metric || "equity"} onChange={set("metric")}>{ACCOUNT_METRICS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
          </label>
          {cond()}
          <label>Amount ($)<input value={f.value} inputMode="decimal" onChange={set("value")} /></label>
        </>}

        {kind === "reminder" && <>
          <label>Remind me in (minutes)<input value={f.minutes} inputMode="decimal" onChange={set("minutes")}
            placeholder={editing ? "leave empty to keep the time" : ""} /></label>
          <p className="hint">Minutes of market time: in a replay this follows the replay clock.</p>
        </>}

        {kind === "calendar" && <>
          <fieldset className="notif-types">
            <legend>For symbols this account holds, tell me about</legend>
            {["dividend", "split", "earnings"].map((ev) => (
              <label key={ev} className="check"><input type="checkbox" checked={f.events.includes(ev)}
                onChange={(e) => setF({ ...f, events: e.target.checked ? [...f.events, ev] : f.events.filter((x: string) => x !== ev) })} />
                {ev === "dividend" ? "Dividend ex-dates" : ev === "split" ? "Stock splits" : "Earnings reports"}</label>
            ))}
          </fieldset>
          <label>Days ahead<input value={f.days_before} inputMode="numeric" onChange={set("days_before")} /></label>
          <p className="hint">Earnings reports come from the simulated research of the fictional companies; a live data feed does not supply them here.</p>
        </>}

        <label>{kind === "reminder" ? "Message" : "Note (optional)"}<input value={f.message} maxLength={200} onChange={set("message")} /></label>
        {!ACCOUNT_ONLY.includes(kind) && kind !== "reminder" && (
          <label className="check"><input type="checkbox" checked={f.scope === "all"} onChange={(e) => setF({ ...f, scope: e.target.checked ? "all" : "account" })} />Notify every account, not just this one</label>
        )}
        {kind === "reminder" && (
          <label className="check"><input type="checkbox" checked={f.scope === "all"} onChange={(e) => setF({ ...f, scope: e.target.checked ? "all" : "account" })} />Show in every account</label>
        )}
        <label className="check"><input type="checkbox" checked={f.repeat} onChange={set("repeat")} />{kind === "reminder" ? "Repeat" : "Re-arm after it fires"}</label>
        {f.repeat && <label>{kind === "reminder" ? "Every (minutes)" : "Cool-down before it can fire again (minutes)"}<input value={f.cooldown} inputMode="decimal" onChange={set("cooldown")} /></label>}
        <label>Expires after (hours, optional)<input value={f.expires} inputMode="decimal" placeholder={f.keepExpiry ? `keeps ${dateTime(f.keepExpiry)}` : "never"} onChange={set("expires")} /></label>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row">
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy} onClick={save}>{editing ? "Save alert" : "Create alert"}</button>
        </div>
      </div>
    </div>
  );
}

const STATUS_LABEL: Record<string, string> = { active: "Active", paused: "Paused", triggered: "Triggered", expired: "Expired" };

export function AlertsTab({ accountId, notify, refreshKey, quotes, positions }: {
  accountId: string; notify: (m: string, bad?: boolean) => void; refreshKey: number; quotes: Record<string, any>; positions: any[];
}) {
  const [alerts, error, reload] = usePoll<any[]>(`/alert-rules?account_id=${encodeURIComponent(accountId)}`, 5000, [refreshKey]);
  const [builder, setBuilder] = useState<null | { editing?: any; seed?: AlertSeed }>(null);
  const [symbol, setSymbol] = useState("");
  const [op, setOp] = useState("above");
  const [price, setPrice] = useState("");
  const [show, setShow] = useState("open");
  const add = async () => {
    try {
      const r = await api("/alert-rules", { method: "POST", body: { kind: "price", symbol, op, price, account_id: accountId } });
      setPrice(""); reload(); notify(`Alert created: ${r.description}`);
    } catch (e: any) { notify(`Could not add the alert: ${e.message}`, true); }
  };
  const patch = async (a: any, body: any, done: string) => {
    try { await api(`/alert-rules/${a.id}`, { method: "PATCH", body }); notify(done); }
    catch (e: any) { notify(`Could not change the alert: ${e.message}`, true); }
    reload();
  };
  const remove = async (a: any) => {
    try { await api(`/alert-rules/${a.id}`, { method: "DELETE" }); notify("Alert deleted"); }
    catch (e: any) { notify(`Could not delete the alert: ${e.message}`, true); }
    reload();
  };
  const rows = useMemo(() => (alerts || []).filter((a) => show === "all" || ["active", "paused"].includes(a.status)).slice().reverse(), [alerts, show]);
  const finished = (alerts || []).length - (alerts || []).filter((a) => ["active", "paused"].includes(a.status)).length;
  return (
    <>
      <div className="toolbar alerts-toolbar">
        <button className="primary sm" onClick={() => setBuilder({})}>New alert…</button>
        <input value={symbol} placeholder="Symbol" aria-label="Alert symbol" onChange={(e) => setSymbol(e.target.value.toUpperCase())} style={{ width: 90 }} />
        <select value={op} aria-label="Condition" onChange={(e) => setOp(e.target.value)}><option value="above">rises to</option><option value="below">falls to</option></select>
        <input value={price} placeholder="Price" aria-label="Alert price" inputMode="decimal" onChange={(e) => setPrice(e.target.value)} style={{ width: 90 }} />
        <button className="ghost sm" onClick={add} disabled={!symbol || !price}>Add price alert</button>
        <select value={show} aria-label="Show alerts" onChange={(e) => setShow(e.target.value)}>
          <option value="open">Active and paused</option><option value="all">All, including finished ({finished})</option>
        </select>
      </div>
      {error && <div className="notice bad">Could not load alerts: {error}</div>}
      {!alerts || alerts.length === 0 ? (
        <p className="muted pad">No price alerts. Add one above, or use New alert for percent moves, volume, indicators, options, positions, the account and reminders. When an alert fires it appears under the bell.</p>
      ) : rows.length === 0 ? <p className="muted pad">No active alerts. Choose "All" to see the ones that have fired or expired.</p> : (
        <table className="alerts-table">
          <thead><tr><th>Alert</th><th>Type</th><th>For</th><th>Status</th><th>Last seen</th><th /></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} data-alert={a.id}>
                <td className="alert-desc"><b>{a.description}</b>
                  {a.message && <div className="muted">Note: {a.message}</div>}
                  {a.note && <div className="muted">{a.note}</div>}
                  {a.repeat && <div className="muted">Re-arms{a.kind === "reminder" ? "" : ` after ${Math.round(a.cooldown / 60)} min cool-down`}</div>}
                  {a.expires_at && <div className="muted">Expires {dateTime(a.expires_at)}</div>}
                </td>
                <td>{KIND_LABEL[a.kind] || a.kind}</td>
                <td>{a.account_id ? "This account" : "All accounts"}</td>
                <td>{STATUS_LABEL[a.status] || a.status}{a.triggered_at ? <div className="muted">Fired {a.fire_count}× · last {dateTime(a.triggered_at)}</div> : null}</td>
                <td className="muted">{a.last_value || "–"}</td>
                <td className="num alert-actions">
                  <button className="ghost sm" onClick={() => setBuilder({ editing: a })}>Edit</button>
                  {a.status === "active" && <button className="ghost sm" onClick={() => patch(a, { status: "paused" }, "Alert paused")}>Pause</button>}
                  {a.status === "paused" && <button className="ghost sm" onClick={() => patch(a, { status: "active" }, "Alert resumed")}>Resume</button>}
                  <button className="ghost sm" onClick={() => remove(a)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {builder && (
        <AlertBuilder accountId={accountId} editing={builder.editing} seed={builder.seed} quotes={quotes} positions={positions}
          onClose={() => setBuilder(null)} onSaved={(m) => { setBuilder(null); notify(m); reload(); }} />
      )}
    </>
  );
}
