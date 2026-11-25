// Global keyboard shortcuts and their help overlay ("?"). Shortcuts never fire while typing in a field, while
// another dialog is open, or with Ctrl, Cmd or Alt held, so the browser's own shortcuts keep working.
import { useEffect, useRef, useState } from "react";
import { api } from "../util";
import { Dialog } from "../Dialog";
import { SHORTCUTS_EVENT, TIMEFRAME_EVENT } from "../Chart";

const TFS = ["1m", "5m", "15m", "1h", "1d", "1w"];
const OPEN = ["held", "working", "partially_filled"];

export const SHORTCUTS: { group: string; keys: string[]; what: string }[] = [
  { group: "General", keys: ["?"], what: "Show or hide this list" },
  { group: "General", keys: ["/"], what: "Search for a symbol" },
  { group: "General", keys: ["J"], what: "Next watchlist symbol" },
  { group: "General", keys: ["K"], what: "Previous watchlist symbol" },
  { group: "Chart", keys: ["1", "–", "6"], what: "Timeframe 1m, 5m, 15m, 1h, 1D, 1W (1 minute to 1 week)" },
  { group: "Chart", keys: ["Delete"], what: "Remove the selected drawing (in draw mode)" },
  { group: "Chart", keys: ["Esc"], what: "Leave the drawing tool, then draw mode" },
  { group: "Order ticket", keys: ["B"], what: "Buy" },
  { group: "Order ticket", keys: ["S"], what: "Sell" },
  { group: "Order ticket", keys: ["T"], what: "Switch between buy and sell" },
  { group: "Order ticket", keys: ["Q"], what: "Enter the quantity" },
  { group: "Order ticket", keys: ["R"], what: "Review the order (placing it still needs a click or Enter)" },
  { group: "Orders", keys: ["Shift", "X"], what: "Cancel all open orders (asks first)" },
];

const typing = (t: EventTarget | null) => {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
};
const ticket = () => document.querySelector<HTMLElement>('section[aria-label="Order ticket"]');

export function Shortcuts({ symbols, symbol, onSelect, accountId, orders, notify, onChanged, onTicket }: {
  symbols: string[]; symbol: string; onSelect: (s: string) => void; accountId: string; orders: any[];
  notify: (text: string, bad?: boolean) => void; onChanged: () => void;
  /** Shows the order ticket first (the phone layout keeps it in the Trade view). */
  onTicket?: () => void;
}) {
  const [help, setHelp] = useState(false);
  const [cancelAll, setCancelAll] = useState<null | { busy?: boolean; error?: string }>(null);
  const state = useRef({ symbols, symbol, onSelect, onTicket });
  state.current = { symbols, symbol, onSelect, onTicket };
  const open = orders.filter((o) => OPEN.includes(o.status));

  useEffect(() => {
    const showHelp = () => setHelp((h) => !h);
    window.addEventListener(SHORTCUTS_EVENT, showHelp);
    return () => window.removeEventListener(SHORTCUTS_EVENT, showHelp);
  }, []);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
      if (typing(e.target)) return;
      const ourDialog = !!document.querySelector(".ct-help-bg");
      if (!ourDialog && document.querySelector('[aria-modal="true"]')) return; // another dialog has the keyboard
      const k = e.key;
      if (ourDialog) {
        if (k === "Escape" || k === "?") { e.preventDefault(); setHelp(false); setCancelAll(null); }
        return;
      }
      const { symbols: list, symbol: cur, onSelect: select, onTicket: show } = state.current;
      // Ticket keys: on a phone, switch to the Trade view first and act once it is shown.
      const onTicketKey = (fn: () => void) => { if (show) { show(); setTimeout(fn, 60); } else fn(); };
      const click = (sel: string) => onTicketKey(() => { const b = ticket()?.querySelector<HTMLButtonElement>(sel); if (b && !b.disabled) b.click(); });
      let done = true;
      if (k === "?") setHelp(true);
      else if (k === "/") document.querySelector<HTMLInputElement>('input[aria-label="Search symbols"]')?.focus();
      else if (/^[1-6]$/.test(k) && !e.shiftKey) {
        const tf = TFS[Number(k) - 1];
        try { localStorage.setItem("pd.tf", tf); } catch {}
        window.dispatchEvent(new CustomEvent(TIMEFRAME_EVENT, { detail: tf }));
      } else if ((k === "j" || k === "k") && list.length) {
        const i = list.indexOf(cur);
        const next = i < 0 ? 0 : (i + (k === "j" ? 1 : -1) + list.length) % list.length;
        select(list[next]);
      } else if (k === "b") click(".side-toggle .buy");
      else if (k === "s") click(".side-toggle .sell");
      else if (k === "t") click(".side-toggle button:not(.on)");
      else if (k === "q") onTicketKey(() => {
        const input = ticket()?.querySelector<HTMLInputElement>("label input[inputmode=decimal]");
        if (input) { input.focus(); input.select(); }
      });
      else if (k === "r") onTicketKey(() => {
        const t = ticket();
        const place = [...(t?.querySelectorAll<HTMLButtonElement>("button") || [])].find((b) => b.textContent === "Place paper order");
        if (place) place.focus(); // already reviewed: the order is placed only by an explicit click or Enter
        else {
          const rev = [...(t?.querySelectorAll<HTMLButtonElement>("button") || [])].find((b) => b.textContent?.startsWith("Review"));
          if (rev && !rev.disabled) rev.click();
          else if (rev) { notify("Enter a quantity before reviewing the order.", true); t?.querySelector<HTMLInputElement>("label input[inputmode=decimal]")?.focus(); }
        }
      }); else if (k === "X" && e.shiftKey) setCancelAll({});
      else done = false;
      if (done) e.preventDefault();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [notify]);

  const doCancelAll = async () => {
    if (!accountId) { setCancelAll({ error: "No account is selected yet." }); return; }
    setCancelAll({ busy: true });
    try {
      const r = await api(`/accounts/${accountId}/orders`, { method: "DELETE" });
      notify(r.canceled ? `Canceled ${r.canceled} open order${r.canceled === 1 ? "" : "s"}` : "There were no open orders to cancel");
      setCancelAll(null);
      onChanged();
    } catch (e: any) {
      setCancelAll({ error: `The orders were not canceled: ${e.message}` });
    }
  };

  if (cancelAll)
    return (
      <Dialog bgClassName="ct-help-bg" className="ct-help" label="Cancel all open orders" destructive
        onClose={() => !cancelAll.busy && setCancelAll(null)}>
          <div className="ct-confirm-body">
            <h3>Cancel all open orders?</h3>
            <p>{open.length === 0 ? "This account has no open orders." : `This cancels ${open.length} open order${open.length === 1 ? "" : "s"} on every symbol in this account.`}</p>
            {cancelAll.error && <div className="notice bad" role="alert">{cancelAll.error}</div>}
            <div className="ct-row">
              <button type="button" className="ct-btn" autoFocus disabled={cancelAll.busy} onClick={() => setCancelAll(null)}>Keep orders</button>
              <button type="button" className="ct-btn primary" disabled={cancelAll.busy || open.length === 0} onClick={doCancelAll}>Cancel all orders</button>
            </div>
          </div>
      </Dialog>
    );
  if (!help) return null;
  const groups = [...new Set(SHORTCUTS.map((s) => s.group))];
  return (
    <Dialog bgClassName="ct-help-bg" className="ct-help" label="Keyboard shortcuts" onClose={() => setHelp(false)}>
        <h3>Keyboard shortcuts <button type="button" className="ct-btn" autoFocus aria-label="Close keyboard shortcuts" onClick={() => setHelp(false)}>×</button></h3>
        <p className="muted">Shortcuts do nothing while you are typing in a field.</p>
        {groups.map((gr) => (
          <section key={gr}>
            <h4>{gr}</h4>
            <dl>
              {SHORTCUTS.filter((s) => s.group === gr).map((s) => (
                <div key={s.what} style={{ display: "contents" }}>
                  <dt>{s.keys.map((k, i) => (k === "–" ? <span key={i}> – </span> : <kbd key={i}>{k}</kbd>))}</dt>
                  <dd>{s.what}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
    </Dialog>
  );
}
