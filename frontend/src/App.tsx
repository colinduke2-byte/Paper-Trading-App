import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Dialog } from "./Dialog";
import { Chart } from "./Chart";
import { Company, DepthTape, OptionChain, QuoteHeader, usePoll, Watchlist } from "./Market";
import { BottomPanel } from "./Panels";
import { feedBanner, Research } from "./Research";
import { Ticket, TicketSeed } from "./Ticket";
import { MobileShell, Screen, TopAction, useSmallScreen } from "./MobileShell";
import { jumpKey, modeKey, ReplayBar, ReplayPicker, replayBanner, useModeSwitch } from "./Replay";
import { SettingsDialog } from "./Settings";
import { api, isOption, money, pct, sessionOk, signed, signedOut, store, time, tone, numText, underlyingOf, usd } from "./util";
import { useSignOutAction } from "./Login";

type Toast = { id: number; text: string; bad?: boolean };

export default function App() {
  const [accounts, setAccounts] = useState<any[]>([]);
  const [accountId, setAccountId] = useState<string>(() => store("pd.account") || "");
  const [symbol, setSymbol] = useState<string>(() => store("pd.symbol") || "MKTX");
  const [watch, setWatch] = useState<string[]>([]);
  const [quotes, setQuotes] = useState<Record<string, any>>({});
  const [summary, setSummary] = useState<any>(null);
  const [positions, setPositions] = useState<any[]>([]);
  const [orders, setOrders] = useState<any[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [connected, setConnected] = useState(false);
  const [fatal, setFatal] = useState("");
  const [view, setView] = useState("Chart");
  const [seed, setSeed] = useState<TicketSeed>({ symbol });
  const [refreshKey, setRefreshKey] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dialog, setDialog] = useState<null | "new" | "reset">(null);
  const [replayPicker, setReplayPicker] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const small = useSmallScreen();
  const [screen, setScreen] = useState<Screen>("Chart"); // the view shown on a small screen
  const signOut = useSignOutAction(); // null unless a passcode is set and this browser had to sign in
  const ws = useRef<WebSocket | null>(null);
  const toastId = useRef(0);

  const notify = useCallback((text: string, bad = false) => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-3), { id, text, bad }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), bad ? 9000 : 5000);
  }, []);

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
        const [list, wl] = await Promise.all([loadAccounts(), api("/watchlists")]);
        if (!list.find((a: any) => a.id === accountId)) setAccountId(list[0]?.id || "");
        setWatch(wl.Main || Object.values(wl)[0] || []);
      } catch (e: any) {
        setFatal(`Cannot reach the PaperDesk server (${e.message}). Check that it is running, then reload.`);
      }
    })();
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
            if (e.type === "alert") notify(`Alert: ${e.alert.symbol} ${e.alert.op === "above" ? "rose to" : "fell to"} ${money(e.last)}`);
            if (e.type === "fill" && e.fill.kind !== "trade") notify(`${e.fill.kind === "reinvestment" ? "Dividend reinvestment" : e.fill.kind === "split_settlement" ? "Split settlement" : `Option ${e.fill.kind}`}: ${e.fill.side} ${e.fill.qty} ${e.fill.symbol} at ${money(e.fill.price)}`);
            if (e.type === "order" && e.order.status === "filled" && e.order.parent_id) notify(`Filled: ${e.order.role?.replace("_", " ") || "order"} on ${e.order.symbol} at ${money(e.order.avg_fill_price)}`);
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

  const saveWatch = (list: string[]) => {
    setWatch(list);
    api("/watchlists/Main", { method: "PUT", body: list }).catch((e) => notify(`Could not save the watchlist: ${e.message}`, true));
  };

  // Switching between live and replay changes the active exchange: reload what belongs to it.
  useModeSwitch(status, async () => {
    const [list, wl, syms] = await Promise.all([loadAccounts(), api("/watchlists"), api("/symbols")]).catch(() => [[], {}, []]);
    setAccountId((cur) => (list.find((a: any) => a.id === cur) ? cur : list[0]?.id || ""));
    const main = wl.Main || Object.values(wl)[0] || [];
    setWatch(main);
    setSymbol((cur) => (syms.some((x: any) => x.symbol === cur) ? cur : main[0] || syms[0]?.symbol || cur));
    setRefreshKey((k) => k + 1);
  });

  const [info] = usePoll<any>(`/fundamentals/${symbol}`, 30000, [modeKey(status)]);
  const ticketPos = positions.find((p) => p.symbol === seed.symbol);
  const stockPos = positions.find((p) => p.symbol === symbol);
  const openForSymbol = orders.filter((o) => o.symbol === symbol && ["working", "partially_filled"].includes(o.status));
  const ticketDesc = isOption(seed.symbol)
    ? positions.find((p) => p.symbol === seed.symbol)?.description || describeOption(seed.symbol) : seed.symbol;

  const closePosition = (p: any) => {
    setSymbol(p.underlying || p.symbol);
    setTimeout(() => setSeed({ symbol: p.symbol, side: p.qty > 0 ? "sell" : "buy", qty: Math.abs(p.qty), nonce: Date.now() }), 0);
    setScreen("Trade");
  };
  const pickSymbol = (s: string) => { setSymbol(s); setScreen("Chart"); };
  const tradeOption = (s: string, side: string, price: number) => {
    setSeed({ symbol: s, side, type: "limit", limit_price: price, qty: 1, nonce: Date.now() });
    setScreen("Trade");
  };
  const startTrade = (side: string) => { setSeed({ symbol, side, nonce: Date.now() }); setScreen("Trade"); };

  if (fatal) return <div className="fatal"><h1>PaperDesk</h1><p>{fatal}</p></div>;

  // Top-bar actions: desktop shows them in the header, the phone layout lists them in its menu.
  const topActions: TopAction[] = [
    { label: "New account", onClick: () => setDialog("new") },
    { label: "Reset", onClick: () => setDialog("reset") },
    ...(status?.replay ? [] : [{ label: "Replay", title: "Practise on a recorded trading session", onClick: () => setReplayPicker(true) }]),
    ...(accountId ? [{ label: "Settings", title: "Account rules: settlement, day trading, interest, margin calls", onClick: () => setSettingsOpen(true) }] : []),
    ...(signOut ? [{ label: signOut.label, onClick: signOut.onClick }] : []),
  ];
  const session = status ? `${status.session === "regular" ? "Market open" : status.session === "closed" ? "Market closed" : status.session === "pre" ? "Pre-market" : "After hours"} · ${time(status.time)}` : "Connecting…";
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
  const marginNotice = summary?.margin_call && (
    <div className="notice bad wide" role="alert">Margin call: account value {usd(summary.equity)} is below the {usd(summary.maintenance_requirement)} maintenance requirement. Only closing orders are accepted until this is resolved.</div>
  );
  const watchlist = <Watchlist symbols={watch} quotes={quotes} selected={symbol} onSelect={pickSymbol} onChange={saveWatch} />;
  const quoteHeader = <QuoteHeader symbol={symbol} quote={quotes[symbol]} info={info} />;
  const mainTabs = (
    <div className="tabs" role="tablist">
      {["Chart", "Options", "Company", "Research"].map((v) => (
        <button key={v} role="tab" aria-selected={v === view} className={v === view ? "on" : ""} onClick={() => setView(v)}>{v}</button>
      ))}
    </div>
  );
  const mainView = (
    <div className="view">
      {view === "Chart" && <Chart key={jumpKey(status)} symbol={symbol} quote={quotes[symbol]} position={stockPos} orders={openForSymbol} />}
      {view === "Options" && <OptionChain symbol={symbol} onTrade={tradeOption} />}
      {view === "Company" && <Company info={info} />}
      {view === "Research" && <Research symbol={underlyingOf(symbol)} />}
    </div>
  );
  const bottom = accountId && (
    <BottomPanel accountId={accountId} positions={positions} orders={orders} refreshKey={refreshKey} quotes={quotes}
      onSelect={pickSymbol} onClose={closePosition} onChanged={() => setRefreshKey((k) => k + 1)} notify={notify} />
  );
  const ticketEl = accountId && (
    <Ticket seed={seed} accountId={accountId} quote={quotes[seed.symbol]} description={ticketDesc} position={ticketPos}
      onPlaced={(m) => { notify(m); setRefreshKey((k) => k + 1); }} />
  );
  const depth = <DepthTape symbol={underlyingOf(symbol)} />;
  const replayBar = <ReplayBar status={status} onStatus={setStatus} notify={notify} />;
  const mode = modeKey(status) || "live"; // remounts the views when switching between live and a replay run
  const overlays = (
    <>
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={t.bad ? "toast bad" : "toast"}>{t.text}</div>)}
      </div>
      {replayPicker && <ReplayPicker onClose={() => setReplayPicker(false)} onStarted={(st, msg) => { setReplayPicker(false); setStatus(st); notify(msg); }} />}
      {settingsOpen && accountId && (
        <SettingsDialog accountId={accountId} summary={summary} onClose={() => setSettingsOpen(false)} onSaved={(m) => notify(m)} />
      )}
      {dialog && (
        <AccountDialog mode={dialog} account={accounts.find((a) => a.id === accountId)} onClose={() => setDialog(null)}
          onDone={async (id, msg) => { setDialog(null); await loadAccounts(); if (id) setAccountId(id); setRefreshKey((k) => k + 1); notify(msg); }} />
      )}
    </>
  );

  if (small) {
    return (
      <MobileShell screen={screen} setScreen={setScreen} accountId={accountId} account={accounts.find((a) => a.id === accountId)}
        summary={summary} connected={connected} session={session} banner={banner} accountSelect={accountSelect} actions={topActions}
        notice={marginNotice} watchlist={watchlist} quote={quoteHeader} mainTabs={mainTabs} mainView={mainView} view={view}
        ticket={ticketEl} depth={depth} bottom={bottom} symbol={symbol} onTrade={startTrade}
        replayBar={status?.replay ? replayBar : null} mode={mode}>
        {overlays}
      </MobileShell>
    );
  }

  return (
    <div className="app" data-account={accountId}>
      <header className="top">
        <div className="brand">PaperDesk</div>
        {banner}
        <div className="top-stats">
          <div><span>Account value</span><b>{usd(summary?.equity)}</b></div>
          <div><span>Day P&amp;L</span><b className={tone(summary?.day_pnl)}>{signed(summary?.day_pnl)}</b></div>
          <div><span>Buying power</span><b>{usd(summary?.buying_power)}</b></div>
          <div><span>Cash</span><b>{usd(summary?.cash)}</b></div>
          {summary?.type === "margin" && <div><span>Margin required</span><b>{usd(summary?.maintenance_requirement)}</b></div>}
        </div>
        <div className="top-right">
          <span className={connected ? "dot ok" : "dot"} title={connected ? "Live connection" : "Reconnecting…"} />
          <span className="muted">{session}</span>
          {accountSelect}
          {topActions.map((a) => <Fragment key={a.label}>{a.element ?? <button className="ghost sm" title={a.title} onClick={a.onClick}>{a.label}</button>}</Fragment>)}
        </div>
      </header>
      {replayBar}
      {marginNotice}
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
  );
}

function describeOption(s: string) {
  const m = s.match(/^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return s;
  return `${m[1]} 20${m[2]}-${m[3]}-${m[4]} ${Number(m[6]) / 1000} ${m[5] === "C" ? "Call" : "Put"}`;
}

function AccountDialog({ mode, account, onClose, onDone }: { mode: "new" | "reset"; account: any; onClose: () => void; onDone: (id: string, msg: string) => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState("margin");
  const [cash, setCash] = useState(mode === "reset" && account ? String(account.starting_cash) : "100000");
  const [error, setError] = useState("");
  const submit = async () => {
    try {
      if (mode === "new") {
        const a = await api("/accounts", { method: "POST", body: { name, type, starting_cash: numText(cash) } });
        onDone(a.id, `Account "${a.name}" created`);
      } else {
        await api(`/accounts/${account.id}/reset`, { method: "POST", body: { starting_cash: numText(cash) } });
        onDone(account.id, `Account "${account.name}" reset to ${usd(Number(numText(cash)))}`);
      }
    } catch (e: any) { setError(e.message); }
  };
  return (
    <Dialog onClose={onClose} labelledBy="account-dialog-title" destructive={mode === "reset"}>
        <h3 id="account-dialog-title">{mode === "new" ? "New paper account" : `Reset "${account?.name}"`}</h3>
        {mode === "reset" && <p className="muted">This erases the account's positions, orders and history and starts again with the cash below. It cannot be undone.</p>}
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
          <button className="primary" onClick={submit}>{mode === "new" ? "Create account" : "Erase and reset"}</button>
        </div>
    </Dialog>
  );
}
