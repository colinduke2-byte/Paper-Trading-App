import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Dialog } from "./Dialog";
import { FeedNotice } from "./FeedNotice";
import { Chart, SHORTCUTS_EVENT } from "./Chart";
import { Shortcuts } from "./chart/Shortcuts";
import { Company, DepthTape, OptionChain, QuoteHeader, usePoll } from "./Market";
import { Watchlists } from "./Watchlists";
import { Markets } from "./Markets";
import { Compare } from "./Compare";
import { BottomPanel } from "./Panels";
import { feedBanner, Research } from "./Research";
import { Ticket, TicketSeed } from "./Ticket";
import { CryptoAbout, CryptoTicket } from "./CryptoTicket";
import { MobileShell, Screen, SCREENS, TopAction, useSmallScreen } from "./MobileShell";
import { jumpKey, modeKey, ReplayBar, ReplayPicker, replayBanner, useModeSwitch } from "./Replay";
import { SettingsDialog } from "./Settings";
import { DataDialog, SaveNotice } from "./DataBackups";
import { BuilderInit, closingLegs, StrategyBuilder } from "./StrategyBuilder";
import { HelpDialog } from "./Help";
import { api, dateTime, describeOption, inReplayTime, isCrypto, isOption, marketableLimit, money, numText, nyTime, OPEN_SETTINGS, pct, price, qty as fmtQty, sessionOk, setReplayTime, shortDateTime, signed, signedOut, signedUsd, store, ticketFocus, time, tone, underlyingOf, usd, zoneLabel } from "./util";
import { ConnectionContext, useConnectionState } from "./connection";
import { ConfirmHost } from "./Confirm";
import { Info } from "./Info";
import { Welcome } from "./Welcome";
import { useSignOutAction } from "./Login";
import { bellLabel, deliver, NotificationBell, NotificationPanel, useNotifications } from "./Notifications";
import { ALERT_AT_PRICE, AlertBuilder, AlertSeed } from "./AlertsTab";

type Toast = { id: number; text: string; bad?: boolean };
const MAIN_VIEWS = ["Chart", "Options", "Company", "Research"];

const FILL_KIND: Record<string, string> = { exercise: "exercised", assignment: "assigned" };

/** Toast text for a stream event, or null when the event needs no message. */
function eventText(e: any): { text: string; bad?: boolean } | null {
  if (e.type === "alert") return { text: `Alert: ${e.alert.symbol} ${e.alert.op === "above" ? "rose to" : "fell to"} ${price(e.last)}.` };
  if (e.type === "lifecycle") {
    if (!e.message) return null;
    return { text: e.message, bad: ["margin_call", "liquidation", "prices_unavailable"].includes(e.kind) && !/ met[:.]/.test(e.message) };
  }
  if (e.type === "fill") {
    const f = e.fill, name = f.description || f.symbol, n = fmtQty(f.qty);
    const bought = f.side === "buy" ? "bought" : "sold";
    if (f.kind === "trade") return null; // the ticket reports its own fills
    if (f.kind === "reinvestment") return { text: `Dividend reinvested: bought ${n} ${name} at ${price(f.price)}.` };
    if (f.kind === "split_settlement") return { text: `Split settled: ${bought} ${n} ${name} at ${price(f.price)}.` };
    if (f.kind === "expiration") return { text: `${name} expired worthless.` };
    if (f.kind === "closed at expiry") return { text: `${name} was closed at expiry for ${price(f.price)} a share (its value in the money).` };
    if (FILL_KIND[f.kind]) {
      // Exercise and assignment book two fills: the option leaves at zero (skipped) and the shares move at the strike.
      if (isOption(f.symbol)) return null;
      return { text: `Option ${FILL_KIND[f.kind]}: you ${bought} ${n} ${name} at ${price(f.price)}.` };
    }
    return { text: `${f.kind.replace(/^./, (c: string) => c.toUpperCase())}: ${bought} ${n} ${name} at ${price(f.price)}.` };
  }
  if (e.type === "order" && e.order.status === "filled") {
    const o = e.order, role = o.role === "take_profit" ? "Take profit" : o.role === "stop_loss" ? "Stop loss" : o.role === "liquidation" ? "Margin call sale" : null;
    if (!role && !o.parent_id) return null;
    return { text: `${role || "Linked order"} filled: ${o.side === "buy" ? "bought" : "sold"} ${fmtQty(o.filled_qty ?? o.qty)} ${o.description || o.symbol} at ${price(o.avg_fill_price)}.`, bad: o.role === "liquidation" };
  }
  return null;
}

/** The account figures shown in the desktop header, each with a definition. */
function accountStats(summary: any) {
  return (
    <>
      <div><span>Account value</span><b>{usd(summary?.equity)}</b></div>
      <div><span>Day P&amp;L <Info term="day_pnl" /></span><b className={tone(summary?.day_pnl)}>{signedUsd(summary?.day_pnl)}</b></div>
      <div title={summary?.type === "margin" ? `Options: ${usd(summary?.option_buying_power)}` : undefined}>
        <span>Buying power <Info term="buying_power" /></span><b>{usd(summary?.buying_power)}</b></div>
      <div><span>Cash</span><b>{usd(summary?.cash)}</b></div>
      {summary?.type === "margin" && <div><span>Maintenance margin <Info term="maintenance_margin" /></span><b>{usd(summary?.maintenance_requirement)}</b></div>}
    </>
  );
}

/** `cur` if the active feed knows it, else the first watchlist symbol (or the first listed symbol). */
async function validSymbol(cur: string, watch: string[]): Promise<string> {
  const known: any[] = cur ? await api(`/symbols/lookup?symbols=${encodeURIComponent(cur)}`).catch(() => []) : [];
  if (known.length) return cur;
  if (watch[0]) return watch[0];
  const first: any[] = await api("/symbols?limit=1").catch(() => []);
  return first[0]?.symbol || cur;
}

export default function App() {
  const [accounts, setAccounts] = useState<any[]>([]);
  const [accountId, setAccountId] = useState<string>(() => store("pd.account") || "");
  const [symbol, setSymbol] = useState<string>(() => store("pd.symbol") || "MKTX");
  // The remembered symbol may belong to the other feed (a fictional one on real data): check it before use.
  const [symbolChecked, setSymbolChecked] = useState(false);
  const [watch, setWatch] = useState<string[]>([]);
  const [quotes, setQuotes] = useState<Record<string, any>>({});
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [orders, setOrders] = useState<any[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [connected, setConnected] = useState(false);
  const [fatal, setFatal] = useState("");
  const [view, setViewState] = useState(() => { const v = store("pd.view"); return v && MAIN_VIEWS.includes(v) ? v : "Chart"; });
  const setView = (v: string) => { setViewState(v); store("pd.view", v); };
  const [seed, setSeed] = useState<TicketSeed>({ symbol });
  const [refreshKey, setRefreshKey] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dialog, setDialog] = useState<null | "new" | "reset" | "delete">(null);
  const [replayPicker, setReplayPicker] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [dataOpen, setDataOpen] = useState(false);
  const [builder, setBuilder] = useState<BuilderInit | null>(null); // the strategy builder, when open
  const [helpOpen, setHelpOpen] = useState(false);
  const small = useSmallScreen();
  // The view shown on a small screen, remembered between visits.
  const [screen, setScreenState] = useState<Screen>(() => { const v = store("pd.screen") as Screen; return v && SCREENS.includes(v) ? v : "Chart"; });
  const setScreen = useCallback((v: Screen) => { setScreenState(v); store("pd.screen", v); }, []);
  const [welcomed, setWelcomed] = useState(() => !!store("pd.welcomed"));
  const signOut = useSignOutAction(); // null unless a passcode is set and this browser had to sign in
  const ws = useRef<WebSocket | null>(null);
  const toastId = useRef(0);
  const notes = useNotifications(accountId, modeKey(status) || "live", connected); // the bell and its log
  const notesRef = useRef(notes);
  notesRef.current = notes;
  const [notesOpen, setNotesOpen] = useState(false);
  const [alertBuilder, setAlertBuilder] = useState<null | { seed?: AlertSeed; editing?: any }>(null);

  const notify = useCallback((text: string, bad = false) => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-3), { id, text, bad }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), bad ? 9000 : 5000);
  }, []);

  const conn = useConnectionState(connected, () => notify("Reconnected. Prices and balances are live again."));

  const loadAccounts = useCallback(async () => {
    const list = await api("/accounts");
    setAccounts(list);
    return list;
  }, []);

  const loadOrders = useCallback(() => {
    if (accountId) api(`/accounts/${accountId}/orders`).then(setOrders).catch(() => {});
  }, [accountId]);

  useEffect(() => {
    (async () => {
      try {
        // The Watchlists panel loads the lists and reports the visible one; here they only pick a valid symbol.
        const [list, wl] = await Promise.all([loadAccounts(), api("/watchlists")]);
        if (!list.find((a: any) => a.id === accountId)) setAccountId(list[0]?.id || "");
        const main: string[] = (wl.Main || Object.values(wl)[0] || []) as string[];
        setSymbol(await validSymbol(symbol, main));
        setSymbolChecked(true);
      } catch (e: any) {
        setFatal("PaperDesk can't reach its server. Check that PaperDesk is still running on your computer (the window where you started it). This page tries again every few seconds.");
      }
    })();
  }, []);

  // While the server cannot be reached at start-up, try again every 3 s and reload once it answers.
  useEffect(() => {
    if (!fatal) return;
    const t = setInterval(() => fetch("/api/status").then((r) => { if (r.ok) location.reload(); }).catch(() => {}), 3000);
    return () => clearInterval(t);
  }, [fatal]);

  useEffect(() => {
    const open = () => setSettingsOpen(true);
    window.addEventListener(OPEN_SETTINGS, open);
    return () => window.removeEventListener(OPEN_SETTINGS, open);
  }, []);

  useEffect(() => { if (accountId) store("pd.account", accountId); setSummary(null); setPositions([]); setOrders([]); loadOrders(); }, [accountId]);
  useEffect(() => { store("pd.symbol", symbol); setSeed({ symbol, nonce: Date.now() }); }, [symbol]);

  const subscribed = useMemo(() => {
    const set = new Set<string>([symbol, seed.symbol, ...watch]);
    for (const p of positions) set.add(p.symbol);
    return [...set].sort();
  }, [symbol, seed.symbol, watch.join(","), positions.map((p) => p.symbol).join(",")]);
  const subKey = subscribed.join(",") + "|" + accountId;
  const subRef = useRef({ subscribed, accountId });
  subRef.current = { subscribed, accountId };

  // Live stream with automatic reconnect.
  useEffect(() => {
    let closed = false, retry: any = null, delay = 500;
    const connect = () => {
      const sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      ws.current = sock;
      sock.onopen = () => {
        delay = 500; setConnected(true);
        sock.send(JSON.stringify({ symbols: subRef.current.subscribed, account: subRef.current.accountId }));
      };
      sock.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type !== "tick") return;
        setStatus(m.status);
        setQuotes((q) => ({ ...q, ...m.quotes }));
        if (m.account && m.account.id === subRef.current.accountId) { setSummary(m.account); setPositions(m.positions || []); }
        if (m.events?.length) {
          setRefreshKey((k) => k + 1);
          for (const e of m.events) {
            if (e.account_id && e.account_id !== subRef.current.accountId) continue; // another account's event
            if (e.type === "notification" && notesRef.current.receive(e.notification)) deliver(e.notification, notify);
            const text = eventText(e);
            if (text) notify(text.text, text.bad);
          }
        }
      };
      sock.onclose = (ev) => {
        setConnected(false);
        if (closed) return;
        if (ev.code === 4401) { signedOut(); return; } // session ended: back to the sign-in screen, no retry
        retry = setTimeout(() => sessionOk().then((ok) => { if (closed) return; if (ok) connect(); else signedOut(); }), delay);
        delay = Math.min(delay * 2, 8000);
      };
      sock.onerror = () => sock.close();
    };
    connect();
    return () => { closed = true; clearTimeout(retry); ws.current?.close(); };
  }, []);

  useEffect(() => {
    if (ws.current?.readyState === 1) ws.current.send(JSON.stringify({ symbols: subscribed, account: accountId }));
  }, [subKey]);

  useEffect(() => { loadOrders(); }, [refreshKey]);
  useEffect(() => { const t = setInterval(loadOrders, 10000); return () => clearInterval(t); }, [loadOrders]);

  // Switching between live and replay changes the active exchange: reload what belongs to it.
  // The account panels wait until the account belongs to the active mode, so a remembered tab never asks
  // the server for the previous mode's account (a 404 right after entering or leaving a replay).
  const [acctMode, setAcctMode] = useState<string | null>(null);
  useEffect(() => { if (status && acctMode === null) setAcctMode(modeKey(status)); }, [status]);
  useModeSwitch(status, async () => {
    const key = modeKey(status) || "live";
    const [list, wl] = await Promise.all([loadAccounts(), api("/watchlists")]).catch(() => [[], {}]);
    setAccountId((cur) => (list.find((a: any) => a.id === cur) ? cur : list[0]?.id || ""));
    setAcctMode(key);
    const main: string[] = (wl.Main || Object.values(wl)[0] || []) as string[];
    setSymbol(await validSymbol(symbol, main));
    setRefreshKey((k) => k + 1);
  });

  // "Alert me at this price": anything can call requestPriceAlert(symbol, price) from AlertsTab.tsx.
  const quotesRef = useRef(quotes);
  quotesRef.current = quotes;
  useEffect(() => {
    const on = (ev: Event) => {
      const { symbol: s, price } = (ev as CustomEvent).detail || {};
      if (!s || !(Number(price) > 0)) return;
      const last = quotesRef.current[s]?.last;
      setAlertBuilder({ seed: { kind: "price", symbol: s, price: Number(price).toFixed(2), op: last != null && Number(price) < last ? "below" : "above" } });
    };
    window.addEventListener(ALERT_AT_PRICE, on);
    return () => window.removeEventListener(ALERT_AT_PRICE, on);
  }, []);
  const editAlert = (id: string) =>
    api(`/alert-rules?account_id=${encodeURIComponent(accountId)}`).then((list: any[]) => {
      const a = list.find((x) => x.id === id);
      if (a) setAlertBuilder({ editing: a }); else notify("That alert has been deleted.", true);
    }).catch((e) => notify(`Could not load the alert: ${e.message}`, true));

  const checkedSymbol = symbolChecked ? symbol : ""; // nothing is fetched for a symbol until it is known to exist
  const [info] = usePoll<any>(checkedSymbol ? `/fundamentals/${checkedSymbol}` : null, 30000, [modeKey(status)]);
  const ticketPos = positions.find((p) => p.symbol === seed.symbol);
  const stockPos = positions.find((p) => p.symbol === symbol);
  const openForSymbol = orders.filter((o) => o.symbol === symbol && ["working", "partially_filled"].includes(o.status));
  const ticketDesc = isOption(seed.symbol)
    ? positions.find((p) => p.symbol === seed.symbol)?.description || describeOption(seed.symbol) : seed.symbol;

  const closePosition = (p: any) => {
    setSymbol(p.underlying || p.symbol);
    setTimeout(() => setSeed({ symbol: p.symbol, side: p.qty > 0 ? "sell" : "buy", qty: Math.abs(p.qty), lots: p.lots, nonce: Date.now() }), 0);
    setScreen("Trade");
    ticketFocus();
  };
  const pickSymbol = (s: string) => { setSymbol(s); setScreen("Chart"); };
  const tradeOption = (s: string, side: string, price: number) => {
    const { limit, note } = marketableLimit(side, price);
    setSeed({ symbol: s, side, type: "limit", limit_price: limit, note, qty: 1, nonce: Date.now() });
    setScreen("Trade");
    ticketFocus();
  };
  const startTrade = (side: string) => { setSeed({ symbol, side, nonce: Date.now() }); setScreen("Trade"); };
  const openBuilder = (init: Omit<BuilderInit, "nonce">) => { if (init.symbol !== symbol) setSymbol(init.symbol); setBuilder({ ...init, nonce: Date.now() }); };
  const closeStrategy = (rows: any[], name: string) => {
    const { legs, qty } = closingLegs(rows);
    openBuilder({ symbol: rows.find((r) => r.underlying)?.underlying || rows[0].symbol, legs, qty, title: `Close ${name.toLowerCase()}` });
  };
  const dismissWelcome = () => { setWelcomed(true); store("pd.welcomed", "1"); };
  const firstTrade = () => {
    const s = watch.includes("MKTX") || !watch.length ? "MKTX" : watch[0];
    dismissWelcome();
    setSymbol(s);
    setTimeout(() => setSeed({ symbol: s, side: "buy", qty: 10, type: "market", nonce: Date.now() }), 0);
    setScreen("Trade");
    ticketFocus();
  };

  if (fatal) return <div className="fatal"><h1>PaperDesk</h1><p role="alert">{fatal}</p></div>;

  // Top-bar actions: desktop shows them in the header, the phone layout lists them in its menu.
  const topActions: TopAction[] = [
    ...(accountId ? [small
      ? { label: bellLabel(notes.unread), onClick: () => setNotesOpen(true) }
      : { label: "Notifications", element: <NotificationBell unread={notes.unread} onClick={() => setNotesOpen(true)} /> }] : []),
    { label: "New account", onClick: () => setDialog("new") },
    { label: "Reset account", danger: true, title: "Erase this account's positions, orders and history and start again", onClick: () => setDialog("reset") },
    ...(accounts.length > 1 && accountId ? [{ label: "Delete account", danger: true, title: "Delete this paper account and its history", onClick: () => setDialog("delete") }] : []),
    ...(status?.replay ? [] : [{ label: "Replay", title: "Practice on a recorded trading session", onClick: () => setReplayPicker(true) }]),
    ...(accountId ? [{ label: "Account rules", title: "Settlement, day trading, interest and margin call rules for this account", onClick: () => setSettingsOpen(true) }] : []),
    { label: "Data", title: "Data and backups: save status, backups, restore, account export and import", onClick: () => setDataOpen(true) },
    { label: "Keyboard shortcuts", title: "Keyboard shortcuts (?)", onClick: () => window.dispatchEvent(new Event(SHORTCUTS_EVENT)) },
    { label: "Help", title: "How PaperDesk works", onClick: () => setHelpOpen(true) },
    ...(signOut ? [{ label: signOut.label, onClick: signOut.onClick }] : []),
  ];
  // Times follow one rule (util.ts): this device's zone, labelled, except in a replay, where everything is ET.
  setReplayTime(!!status?.replay);
  const sessionName = status ? (status.session === "regular" ? "Market open" : status.session === "closed" ? "Market closed" : status.session === "pre" ? "Pre-market" : "After hours") : "";
  const clock = !status ? "" : status.replay ? "" // the replay bar shows the replay clock
    : `${time(status.time)} ${zoneLabel()}${status.session_mode === "us" && zoneLabel() !== "ET" ? ` (${nyTime(status.time)} ET)` : ""}`;
  const session = status ? (clock ? `${sessionName} · ${clock}` : sessionName) : "Connecting…";
  const banner = (
    <div className={status?.replay ? "paper-banner replay" : "paper-banner"} role="note" title={status?.replay ? undefined : feedBanner(status).title}>
      {replayBanner(status) ?? <>Paper trading · no real money{feedBanner(status).text}</>}
    </div>
  );
  const accountSelect = (
    <select value={accountId} aria-label="Account" onChange={(e) => setAccountId(e.target.value)}>
      {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.type})</option>)}
    </select>
  );
  const feedNotice = <FeedNotice status={status} />;
  const unpriced: string[] = summary?.prices_unavailable ?? [];
  const faults: string[] = summary?.faults ?? [];
  const marginNotice = (summary?.margin_call || status?.ui_stale || unpriced.length > 0 || faults.length > 0) ? (
    <>
      {faults.length > 0 && <div className="notice bad wide" role="alert">Something is failing for this account: {faults.join("; ")}. PaperDesk keeps trying; reset or delete the account if it does not clear.</div>}
      {summary?.margin_call && (
        <div className="notice bad wide" role="alert">
          Margin call: your account value ({usd(summary.equity)}) is {usd(Math.max(0, summary.maintenance_requirement - summary.equity))} below
          the maintenance margin ({usd(summary.maintenance_requirement)}). New positions are blocked. Sell or close positions to meet the call
          {summary.liquidation_at ? <>{" "}within the grace period, or PaperDesk will sell them for you at market. The grace period counts only time while the market is open and PaperDesk is running (about {shortDateTime(summary.liquidation_at)} {zoneLabel()} if it keeps running).</> : "."}
          {" "}<Info term="margin_call" />
        </div>
      )}
      {unpriced.length > 0 && <div className="notice wide" role="status">Waiting for prices for {unpriced.join(", ")}: until they arrive the account value leaves those positions out, margin checks are paused and orders that add risk are not accepted.</div>}
      {status?.ui_stale && <div className="notice wide" role="status">This page was built from older sources than the ones in frontend/src. Run "npm run build" in the frontend folder and reload to see the current version.</div>}
    </>
  ) : null;
  const welcome = !welcomed && !status?.replay && accountId && summary && !positions.length && !orders.length ? (
    <Welcome simulated={status?.simulated_data !== false} symbol={watch.includes("MKTX") || !watch.length ? "MKTX" : watch[0]}
      onFirstTrade={firstTrade} onHelp={() => setHelpOpen(true)} onDismiss={dismissWelcome} />
  ) : null;
  const notices = (marginNotice || welcome) ? <>{marginNotice}{welcome}</> : null;
  const saveNotice = <SaveNotice storage={status?.storage} />;
  const watchlist = <Watchlists quotes={quotes} positions={positions} selected={symbol} accountId={accountId} onSelect={pickSymbol}
    onVisible={setWatch} notify={notify} />;
  const openSymbol = (s: string) => { setView("Chart"); pickSymbol(s); }; // from the Markets and Compare views
  const quoteHeader = <QuoteHeader symbol={symbol} quote={quotes[symbol]} info={info} />;
  // A crypto pair has no options, company data or research: it shows Chart and About.
  const crypto = isCrypto(symbol);
  const shown = crypto ? (view === "About" || view === "Markets" ? view : "Chart") : view === "About" ? "Chart" : view;
  const mainTabs = (
    <div className="tabs" role="tablist">
      {(crypto ? ["Chart", "About", "Markets"] : ["Chart", "Options", "Company", "Research", "Markets", "Compare"]).map((v) => (
        <button key={v} role="tab" aria-selected={v === shown} className={v === shown ? "on" : ""} onClick={() => setView(v)}>{v}</button>
      ))}
    </div>
  );
  const mode = modeKey(status) || "live"; // remounts the views when switching between live and a replay run
  const mainView = !checkedSymbol ? <div className="view" /> : (
    <div className="view">
      {shown === "About" && <CryptoAbout info={info} />}
      {shown === "Chart" && <Chart key={jumpKey(status)} symbol={symbol} quote={quotes[symbol]} position={stockPos} orders={openForSymbol}
        accountId={accountId} notify={notify} onOrdersChanged={() => setRefreshKey((k) => k + 1)} scope={status?.replay ? `replay:${status.replay.session_id}` : "live"}
        onTradeAtPrice={(s) => { setSeed({ ...s, nonce: Date.now() }); setScreen("Trade"); }} />}
      {shown === "Options" && <OptionChain symbol={symbol} onTrade={tradeOption} onStrategy={(legs) => openBuilder({ symbol, legs })} />}
      {shown === "Company" && <Company info={info} />}
      {shown === "Research" && <Research symbol={underlyingOf(symbol)} held={positions.map((p) => underlyingOf(p.symbol))} onSelect={setSymbol} />}
      {shown === "Markets" && <Markets onSelect={openSymbol} small={small} />}
      {shown === "Compare" && <Compare symbol={underlyingOf(symbol)} onSelect={openSymbol} />}
    </div>
  );
  const bottom = accountId && (acctMode === null || acctMode === (modeKey(status) || "live")) && (
    <BottomPanel accountId={accountId} positions={positions} orders={orders} refreshKey={refreshKey} quotes={quotes}
      onSelect={pickSymbol} onClose={closePosition} onChanged={() => setRefreshKey((k) => k + 1)} notify={notify}
      onCloseStrategy={closeStrategy} />
  );
  const ticketEl = accountId && (isCrypto(seed.symbol) ? (
    <CryptoTicket seed={seed} accountId={accountId} quote={quotes[seed.symbol]} position={ticketPos} summary={summary}
      onPlaced={(m) => { notify(m); setRefreshKey((k) => k + 1); }} />
  ) : (
    <Ticket seed={seed} accountId={accountId} quote={quotes[seed.symbol]} description={ticketDesc} position={ticketPos}
      onPlaced={(m) => { notify(m); setRefreshKey((k) => k + 1); }} />
  ));
  // Under an option ticket the depth ladder is the underlying stock's, so it says so.
  const depth = checkedSymbol ? <DepthTape symbol={underlyingOf(checkedSymbol)} note={isOption(seed.symbol) ? `${underlyingOf(seed.symbol)} stock, not the option` : undefined} /> : null;
  const replayBar = <ReplayBar status={status} onStatus={setStatus} notify={notify} />;
  const lostBar = conn.stale ? (
    <div className="lost-bar" role="alert">
      <b>Connection to the PaperDesk server lost.</b>{" "}
      Prices and balances below are frozen{status ? ` as of ${time(status.time)} ${zoneLabel()}` : ""}. Reconnecting…
    </div>
  ) : null;
  const overlays = (
    <>
      <Shortcuts symbols={watch} symbol={symbol} onSelect={setSymbol} accountId={accountId} orders={orders} notify={notify}
        onChanged={() => setRefreshKey((k) => k + 1)} onTicket={small ? () => setScreen("Trade") : undefined} />
      <ConfirmHost />
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={t.bad ? "toast bad" : "toast"}>{t.text}</div>)}
      </div>
      {notesOpen && accountId && (
        <NotificationPanel notes={notes} onClose={() => setNotesOpen(false)} onSelect={pickSymbol} onAlert={editAlert} />
      )}
      {alertBuilder && accountId && (
        <AlertBuilder accountId={accountId} seed={alertBuilder.seed} editing={alertBuilder.editing} quotes={quotes} positions={positions}
          onClose={() => setAlertBuilder(null)} onSaved={(m) => { setAlertBuilder(null); notify(m); setRefreshKey((k) => k + 1); }} />
      )}
      {replayPicker && <ReplayPicker onClose={() => setReplayPicker(false)} onStarted={(st, msg) => { setReplayPicker(false); setStatus(st); notify(msg); }} />}
      {dataOpen && (
        <DataDialog accountId={accountId} onClose={() => setDataOpen(false)} notify={notify}
          onRestored={async () => { const list = await loadAccounts(); if (!list.find((a: any) => a.id === accountId)) setAccountId(list[0]?.id || ""); setRefreshKey((k) => k + 1); }} />
      )}
      {builder && accountId && (
        <StrategyBuilder key={builder.nonce} init={builder} accountId={accountId} stockQuote={quotes[builder.symbol]} feed={status?.feed || "feed"}
          onClose={() => setBuilder(null)} onPlaced={(m) => { setBuilder(null); notify(m); setRefreshKey((k) => k + 1); }} />
      )}
      {helpOpen && <HelpDialog onClose={() => setHelpOpen(false)} />}
      {settingsOpen && accountId && (
        <SettingsDialog accountId={accountId} summary={summary} onClose={() => setSettingsOpen(false)} onSaved={(m) => notify(m)} />
      )}
      {dialog && (
        <AccountDialog mode={dialog} account={accounts.find((a) => a.id === accountId)} next={accounts.find((a) => a.id !== accountId)} onClose={() => setDialog(null)}
          onDone={async (id, msg) => { setDialog(null); await loadAccounts(); if (id) setAccountId(id); setRefreshKey((k) => k + 1); notify(msg); }} />
      )}
    </>
  );

  if (small) {
    return (
      <ConnectionContext.Provider value={conn}>
      <MobileShell screen={screen} setScreen={setScreen} accountId={accountId} account={accounts.find((a) => a.id === accountId)}
        summary={summary} connected={connected} session={session} banner={banner} accountSelect={accountSelect} actions={topActions}
        notice={(status?.live_unavailable || status?.storage?.saving_failed) ? <div className="m-notices">{status?.storage?.saving_failed && saveNotice}{status?.live_unavailable && feedNotice}{notices}</div> : notices} watchlist={watchlist} quote={quoteHeader} mainTabs={mainTabs} mainView={mainView} view={shown}
        ticket={ticketEl} depth={depth} bottom={bottom} symbol={symbol} onTrade={startTrade}
        replayBar={status?.replay ? replayBar : null} mode={mode} stale={conn.stale} lostBar={lostBar}>
        {overlays}
      </MobileShell>
      </ConnectionContext.Provider>
    );
  }

  return (
    <ConnectionContext.Provider value={conn}>
    <div className={conn.stale ? "app stale" : "app"} data-account={accountId}>
      <header className="top">
        <div className="brand">PaperDesk</div>
        {banner}
        <div className="top-stats">
          {accountStats(summary)}
        </div>
        <div className="top-right">
          <span className={connected ? "dot ok" : "dot"} role="img" aria-label={connected ? "Live connection" : "Reconnecting"} title={connected ? "Live connection" : "Reconnecting…"} />
          <span className="muted">{session}</span>
          {accountSelect}
          {topActions.map((a) => <Fragment key={a.label}>{a.element ?? <button className={a.danger ? "ghost sm danger-outline" : "ghost sm"} title={a.title} onClick={a.onClick}>{a.label}</button>}</Fragment>)}
        </div>
      </header>
      {lostBar}
      {replayBar}
      {saveNotice}
      {feedNotice}
      {notices}
      <main key={mode}>
        {watchlist}
        <div className="center-col">
          <section className="panel main-view">
            {quoteHeader}
            {mainTabs}
            {mainView}
          </section>
          {bottom}
        </div>
        <div className="right-col">
          {ticketEl}
          {depth}
        </div>
      </main>
      {overlays}
    </div>
    </ConnectionContext.Provider>
  );
}

function AccountDialog({ mode, account, next, onClose, onDone }: { mode: "new" | "reset" | "delete"; account: any; next?: any; onClose: () => void; onDone: (id: string, msg: string) => void }) {
  if (mode === "delete") return <DeleteAccountDialog account={account} next={next} onClose={onClose} onDone={onDone} />;
  const [name, setName] = useState("");
  const [type, setType] = useState("margin");
  const [cash, setCash] = useState(mode === "reset" && account ? String(account.starting_cash) : "100000");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const initialCash = mode === "reset" && account ? String(account.starting_cash) : "100000";
  const dirty = name !== "" || type !== "margin" || cash !== initialCash;
  const submit = async () => {
    setBusy(true);
    try {
      if (mode === "new") {
        const a = await api("/accounts", { method: "POST", body: { name, type, starting_cash: numText(cash) } });
        onDone(a.id, `"${a.name}" created and selected.`);
      } else {
        await api(`/accounts/${account.id}/reset`, { method: "POST", body: { starting_cash: numText(cash) } });
        onDone(account.id, `"${account.name}" was reset to ${usd(Number(numText(cash)))}.`);
      }
    } catch (e: any) { setError(e.message); }
    setBusy(false);
  };
  return (
    <Dialog onClose={onClose} labelledBy="account-dialog-title" destructive={mode === "reset"}>
        <h3 id="account-dialog-title">{mode === "new" ? "New paper account" : "Reset this account"}</h3>
        {mode === "reset" && <p className="dialog-name" title={account?.name}>{account?.name}</p>}
        {mode === "reset" && <p className="muted">This erases the account's positions, orders, history and trade journal and starts again with the cash below. The account's rule settings and tax-lot method are kept. It cannot be undone.</p>}
        {mode === "new" && (
          <>
            <label>Name<input value={name} placeholder="Paper account" onChange={(e) => setName(e.target.value)} autoFocus /></label>
            <label>Type
              <select value={type} onChange={(e) => setType(e.target.value)}>
                <option value="margin">Margin (borrowing and short selling allowed)</option>
                <option value="cash">Cash (no borrowing, no short selling)</option>
              </select>
            </label>
          </>
        )}
        <label>Starting cash<input value={cash} inputMode="decimal" onChange={(e) => setCash(e.target.value)} /></label>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row">
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className={mode === "new" ? "primary" : "danger"} onClick={submit} disabled={busy}>{mode === "new" ? "Create account" : "Erase and reset"}</button>
        </div>
    </Dialog>
  );
}

function DeleteAccountDialog({ account, next, onClose, onDone }: { account: any; next?: any; onClose: () => void; onDone: (id: string, msg: string) => void }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const name = account?.name ?? "";
  const confirm = async () => {
    setBusy(true); setError("");
    try {
      await api(`/accounts/${account.id}`, { method: "DELETE" });
      onDone(next?.id ?? "", `Account "${name}" deleted`);
    } catch (e: any) { setError(e.message); setBusy(false); }
  };
  return (
    <Dialog onClose={onClose} labelledBy="delete-title" destructive>
        <h3 id="delete-title">Delete "{name}"</h3>
        <p className="muted">This permanently deletes the paper account with its positions, open orders, fills, statements and trade journal. It cannot be undone. Type the account name to confirm.</p>
        <label>Account name<input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus aria-label="Type the account name to confirm" /></label>
        {error && <div className="notice bad" role="alert">{error}</div>}
        <div className="row">
          <button className="ghost" onClick={onClose}>Cancel</button>
          <button className="primary danger" disabled={busy || typed.trim() !== name.trim()} onClick={confirm}>Delete account</button>
        </div>
    </Dialog>
  );
}
