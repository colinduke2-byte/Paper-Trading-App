import { useCallback, useEffect, useRef, useState } from "react";
import { api, dateTime, store } from "./util";
import "./alerts.css";

// Notification centre UI: the bell (registered in App.tsx's topActions), the panel, and optional
// browser notifications and sound. The server keeps the log (GET /api/notifications); new entries
// arrive over the WebSocket as {type: "notification"} events and the log is reloaded after a
// reconnect, so nothing is missed while the page was closed or the connection dropped.

export type Notice = {
  id: string; ts: number; account_id: string; category: string; severity: "info" | "success" | "warning" | "critical";
  title: string; text: string; symbol?: string | null; order_id?: string | null; alert_id?: string | null; read: boolean;
};

export const CATEGORY_LABELS: Record<string, string> = {
  alert: "Alerts", reminder: "Reminders", fill: "Fills", order: "Orders", option: "Option exercise and expiry",
  margin: "Margin calls", liquidation: "Liquidation", dividend: "Dividends", split: "Splits", account: "Account charges",
  data: "Data feed",
};
const FILTERS: [string, string][] = [
  ["", "All types"], ["alert,reminder", "Alerts and reminders"], ["fill,order", "Orders and fills"],
  ["margin,liquidation", "Margin and liquidation"], ["dividend,split", "Dividends and splits"], ["option", "Options"],
  ["data", "Data feed"], ["account", "Account charges"],
];
// Shown as a toast as well as in the log. Fills are left out: the order ticket already reports them.
const TOASTED = new Set(["alert", "reminder", "margin", "liquidation", "option", "data", "split", "dividend", "order"]);

// ---- delivery preferences (this browser only) --------------------------------------------------
export type Prefs = { browser: boolean; sound: boolean; off: string[] };
const PREFS_KEY = "pd.notify";

export function loadPrefs(): Prefs {
  try {
    const p = JSON.parse(store(PREFS_KEY) || "{}");
    return { browser: !!p.browser, sound: !!p.sound, off: Array.isArray(p.off) ? p.off : [] };
  } catch { return { browser: false, sound: false, off: [] }; }
}
const savePrefs = (p: Prefs) => store(PREFS_KEY, JSON.stringify(p));

// Why browser notifications cannot be used here, or "" when they can.
export function browserBlocker(): string {
  if (typeof window === "undefined" || !("Notification" in window))
    return "This browser does not support notifications, so they show inside PaperDesk only.";
  if (!window.isSecureContext)
    return "Browser notifications need a secure connection (HTTPS, or this computer's own address). PaperDesk is open over plain HTTP from another device, so notifications show inside the app only.";
  if (Notification.permission === "denied")
    return "Notifications are blocked for this site in the browser's settings. Allow them there to use this.";
  return "";
}

let audio: AudioContext | null = null;
function beep(severity: string) {
  try {
    const Ctx = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!Ctx) return;
    audio = audio || new Ctx();
    const ctx = audio!;
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = severity === "critical" ? 660 : 880;
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    o.connect(g).connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.4);
  } catch { /* sound is optional */ }
}

// Called for each live notification: toast, sound and browser notification as the preferences say.
export function deliver(n: Notice, toast: (text: string, bad?: boolean) => void) {
  const p = loadPrefs();
  if (p.off.includes(n.category)) return;
  if (TOASTED.has(n.category)) toast(`${n.title}: ${n.text}`, n.severity === "critical");
  if (p.sound) beep(n.severity);
  if (p.browser && !browserBlocker() && Notification.permission === "granted" && document.visibilityState !== "visible") {
    try { new Notification(`PaperDesk (paper trading): ${n.title}`, { body: n.text, tag: n.id }); } catch { /* ignore */ }
  }
}

// ---- the log ------------------------------------------------------------------------------------
export function useNotifications(accountId: string, mode: string, connected: boolean) {
  const [items, setItems] = useState<Notice[]>([]);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState("");
  const current = useRef(accountId);
  current.current = accountId;
  const seen = useRef(new Set<string>());

  const reload = useCallback(() => {
    if (!accountId) { setItems([]); setUnread(0); return; }
    api(`/notifications?account_id=${encodeURIComponent(accountId)}&limit=200`)
      .then((r) => {
        if (current.current !== accountId) return;
        seen.current = new Set(r.items.map((n: Notice) => n.id));
        setItems(r.items); setUnread(r.unread); setError("");
      })
      .catch((e) => setError(e.message));
  }, [accountId]);

  // Catch up whenever the account, live/replay mode or the connection changes.
  useEffect(() => { reload(); }, [reload, mode, connected]);

  const receive = useCallback((n: Notice): boolean => {
    if (n.account_id !== current.current || seen.current.has(n.id)) return false;
    seen.current.add(n.id);
    setItems((prev) => [n, ...prev.filter((x) => x.id !== n.id)].slice(0, 200));
    setUnread((u) => u + 1);
    return true;
  }, []);

  const markRead = useCallback(async (ids?: string[], read = true) => {
    if (!current.current) return;
    const r = await api("/notifications/read", { method: "POST", body: { account_id: current.current, ids, read } });
    setUnread(r.unread);
    setItems((prev) => prev.map((n) => (!ids || ids.includes(n.id) ? { ...n, read } : n)));
  }, []);

  return { items, unread, error, reload, receive, markRead };
}

export type NotificationsApi = ReturnType<typeof useNotifications>;

// ---- the bell -----------------------------------------------------------------------------------
export function bellLabel(unread: number) {
  return unread ? `Notifications (${unread} unread)` : "Notifications";
}

export function NotificationBell({ unread, onClick }: { unread: number; onClick: () => void }) {
  return (
    <button className="ghost sm bell" aria-label={bellLabel(unread)} title="Notifications and alerts" onClick={onClick}>
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0" /></svg>
      {unread > 0 && <span className="bell-count" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
    </button>
  );
}

// ---- the panel ----------------------------------------------------------------------------------
export function NotificationPanel({ notes, onClose, onSelect, onAlert }: {
  notes: NotificationsApi; onClose: () => void; onSelect: (symbol: string) => void; onAlert: (alertId: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [settings, setSettings] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  const cats = filter ? filter.split(",") : null;
  const rows = notes.items.filter((n) => (!cats || cats.includes(n.category)) && (!unreadOnly || !n.read));
  const act = (p: Promise<any>) => p.catch((e) => setError(`Could not update notifications: ${e.message}`));
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal notif-panel" role="dialog" aria-modal="true" aria-label="Notifications" onClick={(e) => e.stopPropagation()}>
        <div className="notif-head">
          <h3>Notifications</h3>
          <button className="ghost sm" onClick={() => setSettings((s) => !s)} aria-expanded={settings}>Delivery</button>
          <button className="ghost sm" onClick={onClose}>Close</button>
        </div>
        {settings && <DeliverySettings />}
        <div className="notif-tools">
          <select value={filter} aria-label="Notification type" onChange={(e) => setFilter(e.target.value)}>
            {FILTERS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <label className="check"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />Unread only</label>
          <button className="ghost sm" disabled={!notes.unread} onClick={() => act(notes.markRead())}>Mark all read</button>
        </div>
        {(error || notes.error) && <div className="notice bad" role="alert">{error || `Could not load notifications: ${notes.error}`}</div>}
        {rows.length === 0 ? (
          <p className="muted pad">{notes.items.length ? "Nothing matches this filter." : "No notifications yet. Fills, alerts, margin calls, dividends and other events appear here."}</p>
        ) : (
          <ul className="notif-list" aria-label="Notification list">
            {rows.map((n) => (
              <li key={n.id} className={`notif sev-${n.severity}${n.read ? "" : " unread"}`} data-id={n.id}>
                <div className="notif-title">
                  {!n.read && <span className="notif-dot" aria-label="Unread" role="img" />}
                  <b>{n.title}</b>
                  <span className="muted">{dateTime(n.ts)}</span>
                </div>
                <div className="notif-text">{n.text}</div>
                <div className="notif-actions">
                  {n.symbol && <button className="ghost sm" onClick={() => { act(notes.markRead([n.id])); onSelect(n.symbol!); onClose(); }}>Show {n.symbol}</button>}
                  {n.alert_id && <button className="ghost sm" onClick={() => { act(notes.markRead([n.id])); onClose(); onAlert(n.alert_id!); }}>Edit alert</button>}
                  <button className="ghost sm" onClick={() => act(notes.markRead([n.id], !n.read))}>{n.read ? "Mark unread" : "Mark read"}</button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function DeliverySettings() {
  const [p, setP] = useState<Prefs>(loadPrefs);
  const [blocker, setBlocker] = useState(browserBlocker);
  const [msg, setMsg] = useState("");
  const update = (next: Prefs) => { setP(next); savePrefs(next); };
  const allow = async () => {
    if (blocker) return;
    try {
      const r = await Notification.requestPermission();
      setBlocker(browserBlocker());
      if (r === "granted") { update({ ...p, browser: true }); setMsg("Browser notifications are on. They appear while PaperDesk is in the background."); }
      else setMsg("The browser did not allow notifications, so they show inside PaperDesk only.");
    } catch (e: any) { setMsg(`The browser refused the request: ${e.message}`); }
  };
  const granted = !blocker && typeof Notification !== "undefined" && Notification.permission === "granted";
  return (
    <div className="notif-settings">
      <p className="muted">These settings are kept in this browser only. Every notification is always kept in the list below.</p>
      {blocker ? <div className="notice" role="note">{blocker}</div> : granted ? (
        <label className="check"><input type="checkbox" checked={p.browser} onChange={(e) => update({ ...p, browser: e.target.checked })} />Browser notifications while PaperDesk is in the background</label>
      ) : (
        <button className="ghost sm" onClick={allow}>Allow browser notifications…</button>
      )}
      <label className="check"><input type="checkbox" checked={p.sound} onChange={(e) => { update({ ...p, sound: e.target.checked }); if (e.target.checked) beep("info"); }} />Play a sound</label>
      {msg && <p className="muted" role="status">{msg}</p>}
      <fieldset className="notif-types">
        <legend>Pop-ups, sound and browser notifications for</legend>
        {Object.entries(CATEGORY_LABELS).map(([k, label]) => (
          <label key={k} className="check">
            <input type="checkbox" checked={!p.off.includes(k)}
              onChange={(e) => update({ ...p, off: e.target.checked ? p.off.filter((x) => x !== k) : [...p.off, k] })} />{label}
          </label>
        ))}
      </fieldset>
    </div>
  );
}
