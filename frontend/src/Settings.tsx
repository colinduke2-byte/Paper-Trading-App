import { useEffect, useState } from "react";
import { confirmAction } from "./Confirm";
import { Info } from "./Info";
import { api, shortDateTime, usd, zoneLabel } from "./util";

type Field = { key: string; type: "bool" | "number"; default: any; label: string; help: string; min: string | null; max: string | null };

const MARGIN_ONLY = new Set([
  "pattern_day_trader", "pdt_min_equity", "margin_interest", "margin_interest_rate", "borrow_fee", "borrow_fee_rate",
  "margin_call_liquidation", "margin_call_grace_hours",
]);
const CASH_ONLY = new Set(["settled_cash_only"]);
// A number setting belongs to the toggle above it; it is disabled while that rule is off.
const S = {
  modal: { width: 520, maxWidth: "calc(100vw - 24px)", maxHeight: "90vh", overflowY: "auto" as const },
  stats: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 8 },
  stat: { display: "flex", flexDirection: "column" as const, fontSize: 11 },
  list: { display: "flex", flexDirection: "column" as const, gap: 10 },
  text: { display: "flex", flexDirection: "column" as const, gap: 1 },
  num: { display: "grid", gridTemplateColumns: "1fr 110px", gap: 10, alignItems: "center" },
  help: { fontSize: 12 },
};
const PARENT: Record<string, string> = {
  pdt_min_equity: "pattern_day_trader", margin_interest_rate: "margin_interest", borrow_fee_rate: "borrow_fee",
  margin_call_grace_hours: "margin_call_liquidation",
};

/** Per-account rule settings: dividends, settlement, pattern day trader, interest, borrow fees, margin calls. */
export function SettingsDialog({ accountId, summary, onClose, onSaved }: {
  accountId: string; summary: any; onClose: () => void; onSaved: (msg: string) => void;
}) {
  const [fields, setFields] = useState<Field[]>([]);
  const [values, setValues] = useState<Record<string, any>>({});
  const [type, setType] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<Record<string, any>>({});
  const [name, setName] = useState("");
  const dirty = Object.keys(values).some((k) => String(values[k]) !== String(saved[k]));

  const load = (r: any) => { setFields(r.fields); setValues(r.settings); setSaved(r.settings); setType(r.account_type); if (r.account_name) setName(r.account_name); };
  useEffect(() => { api(`/accounts/${accountId}`).then((a) => setName(a.name || "")).catch(() => {}); }, [accountId]);
  // Closing with unsaved edits asks first, so a stray click on the backdrop does not lose them.
  const close = async () => {
    if (dirty && !(await confirmAction({ title: "Discard your changes?", body: "The rules you changed have not been saved.", confirm: "Discard changes", cancel: "Keep editing", danger: true }))) return;
    onClose();
  };
  useEffect(() => { api(`/accounts/${accountId}/settings`).then(load).catch((e) => setError(e.message)); }, [accountId]);

  const save = async () => {
    setBusy(true); setError("");
    try {
      load(await api(`/accounts/${accountId}/settings`, { method: "PUT", body: values }));
      onSaved(name ? `Rules saved for "${name}".` : "Rules saved.");
    } catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };
  const reset = async () => {
    if (!(await confirmAction({ title: "Restore the default rules?", body: "Every rule for this account goes back to its default and is saved straight away. Your changes to these settings are replaced.", confirm: "Restore default rules", danger: true }))) return;
    setBusy(true); setError("");
    try { load(await api(`/accounts/${accountId}/settings/reset`, { method: "POST" })); onSaved("Default rules restored."); }
    catch (e: any) { setError(e.message); } finally { setBusy(false); }
  };

  const applies = (f: Field) => !(type === "cash" && MARGIN_ONLY.has(f.key)) && !(type === "margin" && CASH_ONLY.has(f.key));
  const shown = fields.filter(applies);
  const hidden = fields.length - shown.length;

  return (
    <div className="modal-bg" onClick={close}>
      <div className="modal" style={S.modal} role="dialog" aria-modal="true" aria-labelledby="settings-title" onClick={(e) => e.stopPropagation()}>
        <h3 id="settings-title">Account rules{name ? <span className="dialog-name" title={name}>{name}</span> : null}</h3>
        {summary && (
          <div style={S.stats}>
            <div style={S.stat}><span className="muted">Settled cash <Info term="settled_cash" /></span><b>{usd(summary.settled_cash)}</b></div>
            <div style={S.stat}><span className="muted">Settling (from recent sales)</span><b>{usd(summary.unsettled_cash)}</b></div>
            {type === "margin" && <div style={S.stat}><span className="muted">Day trades (last 5 business days) <Info term="day_trades" /></span><b>{summary.day_trades ?? "–"}{summary.day_trades_left != null ? ` · ${summary.day_trades_left} left` : ""}</b></div>}
            {type === "margin" && <div style={S.stat}><span className="muted">Accrued interest</span><b>{usd(summary.accrued_interest)}</b></div>}
            {type === "margin" && <div style={S.stat}><span className="muted">Accrued borrow fee</span><b>{usd(summary.accrued_borrow_fee)}</b></div>}
            {summary.pending_dividends ? <div style={S.stat}><span className="muted">Dividends pending</span><b>{usd(summary.pending_dividends)}</b></div> : null}
          </div>
        )}
        {summary?.prices_unavailable?.length > 0 && (
          <div className="notice" role="status">Prices unavailable for {summary.prices_unavailable.join(", ")}: margin checks are paused until they return.</div>
        )}
        {summary?.liquidation_at && (
          <div className="notice bad" role="alert">If the margin call is not met by {shortDateTime(summary.liquidation_at)} {zoneLabel()}, PaperDesk will start selling positions at market.</div>
        )}
        {!fields.length && !error && <p className="muted">Loading settings…</p>}
        <div style={S.list}>
          {shown.map((f) => {
            const off = PARENT[f.key] && values[PARENT[f.key]] === false;
            return f.type === "bool" ? (
              <label key={f.key} className="check" title={f.help}>
                <input type="checkbox" checked={!!values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.checked })} />
                <span style={S.text}><b>{f.label}</b><small className="muted" style={S.help}>{f.help}</small></span>
              </label>
            ) : (
              <label key={f.key} style={{ ...S.num, opacity: off ? 0.5 : 1 }} title={f.help}>
                <span style={S.text}><span style={{ color: "var(--text)" }}>{f.label}</span><small className="muted" style={S.help}>{f.help}</small></span>
                <input inputMode="decimal" disabled={!!off} value={values[f.key] ?? ""} aria-label={f.label}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
              </label>
            );
          })}
        </div>
        {hidden > 0 && <p className="muted">Rules that only apply to {type === "cash" ? "margin" : "cash"} accounts are hidden.</p>}
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row">
          <button className="ghost" onClick={reset} disabled={busy}>Restore default rules</button>
          <button className="ghost" onClick={close}>{dirty ? "Cancel" : "Close"}</button>
          <button className="primary" onClick={save} disabled={busy || !fields.length || !dirty} title={dirty ? undefined : "Nothing has changed yet"}>Save</button>
        </div>
      </div>
    </div>
  );
}
