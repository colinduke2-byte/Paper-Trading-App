import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chart } from "./Chart";
import { Company, DepthTape, OptionChain, QuoteHeader, usePoll, Watchlist } from "./Market";
import { BottomPanel } from "./Panels";
import { Ticket, TicketSeed } from "./Ticket";
import { SettingsDialog } from "./Settings";
import { api, isOption, money, pct, signed, store, time, tone, underlyingOf, usd } from "./util";

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
  const [settingsOpen, setSettingsOpen] = useState(false);
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
            if (e.type === "fill" && e.fill.kind !== "trade") notify(`Option ${e.fill.kind}: ${e.fill.side} ${e.fill.qty} ${e.fill.symbol} at ${money(e.fill.price)}`);
            if (e.type === "order" && e.order.status === "filled" && e.order.parent_id) notify(`Filled: ${e.order.role?.replace("_", " ") || "order"} on ${e.order.symbol} at ${money(e.order.avg_fill_price)}`);
          }
        }
      };
      sock.onclose = () => { setConnected(false); if (!closed) { retry = setTimeout(connect, delay); delay = Math.min(delay * 2, 8000); } };
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

  const [info] = usePoll<any>(`/fundamentals/${symbol}`, 30000);
  const ticketPos = positions.find((p) => p.symbol === seed.symbol);
  const stockPos = positions.find((p) => p.symbol === symbol);
  const openForSymbol = orders.filter((o) => o.symbol === symbol && ["working", "partially_filled"].includes(o.status));
  const ticketDesc = isOption(seed.symbol)
    ? positions.find((p) => p.symbol === seed.symbol)?.description || describeOption(seed.symbol) : seed.symbol;

  const closePosition = (p: any) => {
    setSymbol(p.underlying || p.symbol);
    setTimeout(() => setSeed({ symbol: p.symbol, side: p.qty > 0 ? "sell" : "buy", qty: Math.abs(p.qty), nonce: Date.now() }), 0);
  };

  if (fatal) return <div className="fatal"><h1>PaperDesk</h1><p>{fatal}</p></div>;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">PaperDesk</div>
        <div className="paper-banner" role="note">
          Paper trading · no real money{status?.simulated_data ? " · simulated market data" : status?.delay_seconds ? ` · data delayed ${status.delay_seconds / 60} min` : ""}
        </div>
        <div className="top-stats">
          <div><span>Account value</span><b>{usd(summary?.equity)}</b></div>
          <div><span>Day P&amp;L</span><b className={tone(summary?.day_pnl)}>{signed(summary?.day_pnl)}</b></div>
          <div><span>Buying power</span><b>{usd(summary?.buying_power)}</b></div>
          <div><span>Cash</span><b>{usd(summary?.cash)}</b></div>
          {summary?.type === "margin" && <div><span>Margin required</span><b>{usd(summary?.maintenance_requirement)}</b></div>}
        </div>
        <div className="top-right">
          <span className={connected ? "dot ok" : "dot"} title={connected ? "Live connection" : "Reconnecting…"} />
          <span className="muted">{status ? `${status.session === "regular" ? "Market open" : status.session === "closed" ? "Market closed" : status.session === "pre" ? "Pre-market" : "After hours"} · ${time(status.time)}` : "Connecting…"}</span>
          <select value={accountId} aria-label="Account" onChange={(e) => setAccountId(e.target.value)}>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.type})</option>)}
          </select>
          <button className="ghost sm" onClick={() => setDialog("new")}>New account</button>
          <button className="ghost sm" onClick={() => setDialog("reset")}>Reset</button>
          <button className="ghost sm" onClick={() => setSettingsOpen(true)} disabled={!accountId}>Settings</button>
        </div>
      </header>
      {summary?.margin_call && (
        <div className="notice bad wide" role="alert">Margin call: account value {usd(summary.equity)} is below the {usd(summary.maintenance_requirement)} maintenance requirement. Only closing orders are accepted until this is resolved.</div>
      )}
      <main>
        <Watchlist symbols={watch} quotes={quotes} selected={symbol} onSelect={setSymbol} onChange={saveWatch} />
        <div className="center-col">
          <section className="panel main-view">
            <QuoteHeader symbol={symbol} quote={quotes[symbol]} info={info} />
            <div className="tabs" role="tablist">
              {["Chart", "Options", "Company"].map((v) => (
                <button key={v} role="tab" aria-selected={v === view} className={v === view ? "on" : ""} onClick={() => setView(v)}>{v}</button>
              ))}
            </div>
            <div className="view">
              {view === "Chart" && <Chart symbol={symbol} quote={quotes[symbol]} position={stockPos} orders={openForSymbol} />}
              {view === "Options" && <OptionChain symbol={symbol} onTrade={(s, side, price) => setSeed({ symbol: s, side, type: "limit", limit_price: price, qty: 1, nonce: Date.now() })} />}
              {view === "Company" && <Company info={info} />}
            </div>
          </section>
          {accountId && (
            <BottomPanel accountId={accountId} positions={positions} orders={orders} refreshKey={refreshKey} quotes={quotes}
              onSelect={setSymbol} onClose={closePosition} onChanged={() => setRefreshKey((k) => k + 1)} notify={notify} />
          )}
        </div>
        <div className="right-col">
          {accountId && (
            <Ticket seed={seed} accountId={accountId} quote={quotes[seed.symbol]} description={ticketDesc} position={ticketPos}
              onPlaced={(m) => { notify(m); setRefreshKey((k) => k + 1); }} />
          )}
          <DepthTape symbol={underlyingOf(symbol)} />
        </div>
      </main>
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={t.bad ? "toast bad" : "toast"}>{t.text}</div>)}
      </div>
      {settingsOpen && accountId && (
        <SettingsDialog accountId={accountId} summary={summary} onClose={() => setSettingsOpen(false)} onSaved={(m) => notify(m)} />
      )}
      {dialog && (
        <AccountDialog mode={dialog} account={accounts.find((a) => a.id === accountId)} onClose={() => setDialog(null)}
          onDone={async (id, msg) => { setDialog(null); await loadAccounts(); if (id) setAccountId(id); setRefreshKey((k) => k + 1); notify(msg); }} />
      )}
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
        const a = await api("/accounts", { method: "POST", body: { name, type, starting_cash: cash } });
        onDone(a.id, `Account "${a.name}" created`);
      } else {
        await api(`/accounts/${account.id}/reset`, { method: "POST", body: { starting_cash: cash } });
        onDone(account.id, `Account "${account.name}" reset to ${usd(Number(cash))}`);
      }
    } catch (e: any) { setError(e.message); }
  };
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h3>{mode === "new" ? "New paper account" : `Reset "${account?.name}"`}</h3>
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
      </div>
    </div>
  );
}
