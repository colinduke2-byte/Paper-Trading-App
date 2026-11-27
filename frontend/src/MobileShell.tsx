import { ReactNode, useEffect, useState } from "react";
import { Dialog } from "./Dialog";
import { signed, signedUsd, tone, usd } from "./util";
import { Info } from "./Info";
import "./mobile.css";

// Small-screen layout: a compact top bar, one full-height view at a time and a bottom tab bar.
// The views are built from the same elements as the desktop layout (App passes them in).
//
// How to add things so they also work on a phone (no change to this file needed):
// - A new tab next to Positions, Orders, ...: add it to TABS in Panels.tsx. The Account view renders
//   BottomPanel as it is, so the tab appears there automatically (the tab strip scrolls sideways).
// - A new top-bar action (a Settings button, say): add an entry to `topActions` in App.tsx, either
//   { label, onClick } or { label, element }. Desktop shows it in the header; here it is a button in
//   the menu (an onClick entry closes the menu first). Make any dialog it opens work at 390 px.
// - Something shown under the header in a special mode (like the replay bar): pass it in as a prop
//   and render it in the "strip" grid row below; the views shrink to make room.
// Controls in the views get 44 px touch targets and 16 px input text from mobile.css automatically.

export type Screen = "Watchlist" | "Chart" | "Trade" | "Account";
export const SCREENS: Screen[] = ["Watchlist", "Chart", "Trade", "Account"];

// A top-bar entry. Desktop renders it in the header; the phone layout lists it in the menu.
export type TopAction = { label: string; onClick?: () => void; element?: ReactNode; title?: string; danger?: boolean };

// Phones in portrait (below 900 px), and phones in landscape (short and not very wide).
export const SMALL_QUERY = "(max-width: 899px), (max-height: 540px) and (max-width: 1199px)";

export function useSmallScreen() {
  const get = () => typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(SMALL_QUERY).matches;
  const [small, setSmall] = useState(get);
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(SMALL_QUERY);
    const on = () => setSmall(mq.matches);
    mq.addEventListener ? mq.addEventListener("change", on) : mq.addListener(on);
    on();
    return () => { mq.removeEventListener ? mq.removeEventListener("change", on) : mq.removeListener(on); };
  }, []);
  return small;
}

// True while the on-screen keyboard is up: a text field has focus and the visible area shrank.
function useKeyboardOpen() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const vv = window.visualViewport;
    let width = window.innerWidth, base = window.innerHeight;
    const check = () => {
      if (window.innerWidth !== width) { width = window.innerWidth; base = window.innerHeight; }
      base = Math.max(base, window.innerHeight);
      const visible = vv ? vv.height * (vv.scale || 1) : window.innerHeight;
      const el = document.activeElement as HTMLInputElement | null;
      const typing = !!el && (el.tagName === "TEXTAREA" ||
        (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "range"].includes(el.type)));
      setOpen(typing && base - visible > 150);
    };
    const later = () => setTimeout(check, 60);
    vv?.addEventListener("resize", check);
    window.addEventListener("resize", check);
    document.addEventListener("focusin", later);
    document.addEventListener("focusout", later);
    return () => {
      vv?.removeEventListener("resize", check);
      window.removeEventListener("resize", check);
      document.removeEventListener("focusin", later);
      document.removeEventListener("focusout", later);
    };
  }, []);
  return open;
}

const ICONS: Record<Screen, string> = {
  Watchlist: "M4 6h16M4 12h16M4 18h10",
  Chart: "M4 19V5M4 19h16M7 15l4-5 3 3 5-7",
  Trade: "M7 4v16M7 4l-3 3M7 4l3 3M17 20V4M17 20l-3-3M17 20l3-3",
  Account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 20c1.5-4 4.5-6 8-6s6.5 2 8 6",
};

function Icon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>
  );
}

type ChainSide = "calls" | "puts" | "both";

export function MobileShell(p: {
  screen: Screen; setScreen: (s: Screen) => void;
  accountId: string; account: any; summary: any; connected: boolean; session: string;
  banner: ReactNode; accountSelect: ReactNode; actions: TopAction[]; notice: ReactNode;
  watchlist: ReactNode; quote: ReactNode; mainTabs: ReactNode; mainView: ReactNode; view: string;
  ticket: ReactNode; depth: ReactNode; bottom: ReactNode; symbol: string;
  onTrade: (side: "buy" | "sell") => void; children?: ReactNode;
  replayBar?: ReactNode; mode?: string; // the replay controls (only while a replay runs) and a key that changes on live/replay switches
  stale?: boolean; lostBar?: ReactNode; // the live stream is down: frozen numbers are dimmed and the bar says why
}) {
  const [menu, setMenu] = useState(false);
  const [replayMore, setReplayMore] = useState(false);
  const [side, setSide] = useState<ChainSide>("calls");
  const keyboard = useKeyboardOpen();
  const s = p.summary;

  useEffect(() => {
    if (!menu) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [menu]);

  const stat = (label: ReactNode, value: ReactNode, cls = "") => <div><span>{label}</span><b className={cls}>{value}</b></div>;

  return (
    <div className={"m-shell" + (keyboard ? " m-kb" : "") + (p.stale ? " stale" : "")} data-account={p.accountId} data-screen={p.screen}>
      <header className="m-top">
        <div className="m-top-row">
          <div className="m-brand">
            <div className="m-brand-name">
              <span className={p.connected ? "dot ok" : "dot"} role="img" aria-label={p.connected ? "Live connection" : "Reconnecting"} />
              PaperDesk
            </div>
            <span className="m-session">{p.session}</span>
          </div>
          <div className="m-stats">
            {stat("Account value", usd(s?.equity))}
            {stat("Day P&L", signedUsd(s?.day_pnl), tone(s?.day_pnl))}
          </div>
          <button className="m-menu-btn" aria-label="Menu" aria-expanded={menu} aria-haspopup="dialog" onClick={() => setMenu(true)}>
            <Icon d="M4 7h16M4 12h16M4 17h16" />
          </button>
        </div>
        <div className="m-top-row2">{p.banner}</div>
      </header>
      {p.replayBar && (
        <div className={"m-replay" + (replayMore ? " open" : "")}>
          {p.replayBar}
          <button className="ghost m-replay-more" aria-expanded={replayMore} aria-label={replayMore ? "Fewer replay controls" : "More replay controls"}
            onClick={() => setReplayMore((x) => !x)}><Icon d="M6 9l6 6 6-6" /></button>
        </div>
      )}
      {(p.lostBar || p.notice) && <div className="m-notices">{p.lostBar}{p.notice}</div>}

      <main className="m-views" key={p.mode}>
        <section className="m-view m-watch" hidden={p.screen !== "Watchlist"} aria-label="Watchlist view">{p.watchlist}</section>

        <section className="m-view m-chart" data-view={p.view} hidden={p.screen !== "Chart"} aria-label="Chart view">
          <div className="m-quote">
            {p.quote}
            <div className="m-tradebtns">
              <button className="primary buy" aria-label={`Buy ${p.symbol}`} onClick={() => p.onTrade("buy")}>Buy</button>
              <button className="primary sell" aria-label={`Sell ${p.symbol}`} onClick={() => p.onTrade("sell")}>Sell</button>
            </div>
          </div>
          {p.mainTabs}
          {p.view === "Options" && (
            <div className="m-chainside">
              <div className="seg" role="group" aria-label="Option side">
                {(["calls", "puts", "both"] as ChainSide[]).map((k) => (
                  <button key={k} className={k === side ? "on" : ""} aria-pressed={k === side} onClick={() => setSide(k)}>
                    {k === "calls" ? "Calls" : k === "puts" ? "Puts" : "Both"}
                  </button>
                ))}
              </div>
              <span className="m-chainhint">Tap an ask to buy or a bid to sell. Each contract covers 100 shares.</span>
            </div>
          )}
          <div className={`m-main m-main-${p.view.toLowerCase()} m-side-${side}`}>{p.mainView}</div>
        </section>

        <section className="m-view m-trade" hidden={p.screen !== "Trade"} aria-label="Trade view">
          {p.ticket}
          {p.depth}
        </section>

        <section className="m-view m-account" hidden={p.screen !== "Account"} aria-label="Account view">
          <div className="m-acct">
            <div className="m-acct-name">
              <span>{p.account ? p.account.name : "No account"}</span>
              {p.account && <span className="tag">{p.account.type === "margin" ? "margin account" : "cash account"}</span>}
            </div>
            <div className="m-acct-stats">
              {stat(<>Buying power <Info term="buying_power" /></>, usd(s?.buying_power))}
              {stat("Cash", usd(s?.cash))}
              {s?.type === "margin" && stat(<>Maintenance margin <Info term="maintenance_margin" /></>, usd(s?.maintenance_requirement))}
            </div>
          </div>
          {p.bottom}
        </section>
        {p.children}
      </main>

      <nav className="m-nav" aria-label="Views">
        {SCREENS.map((v) => (
          <button key={v} aria-current={v === p.screen ? "page" : undefined} className={v === p.screen ? "on" : ""} onClick={() => p.setScreen(v)}>
            <Icon d={ICONS[v]} /><span>{v}</span>
          </button>
        ))}
      </nav>

      {menu && (
        <Dialog onClose={() => setMenu(false)} bgClassName="m-sheet-bg" className="m-sheet" label="Menu">
            <div className="m-sheet-head">
              <h3>Account</h3>
              <button className="ghost" onClick={() => setMenu(false)}>Close</button>
            </div>
            {/* Picking another account closes the menu. */}
            <div className="m-sheet-account" onChange={() => setMenu(false)}>{p.accountSelect}</div>
            <div className="m-acct-stats">
              {stat("Account value", usd(s?.equity))}
              {stat("Day P&L", signedUsd(s?.day_pnl), tone(s?.day_pnl))}
              {stat("Buying power", usd(s?.buying_power))}
              {stat("Cash", usd(s?.cash))}
              {s?.type === "margin" && stat("Maintenance margin", usd(s?.maintenance_requirement))}
            </div>
            <div className="m-sheet-actions">
              {p.actions.filter((a) => !a.danger).map((a) => a.element
                ? <div key={a.label} className="m-sheet-item">{a.element}</div>
                : <button key={a.label} className="ghost" title={a.title} onClick={() => { setMenu(false); a.onClick?.(); }}>{a.label}</button>)}
            </div>
            {/* Destructive actions sit apart from the everyday ones, so a slip of the thumb cannot reach them. */}
            {p.actions.some((a) => a.danger) && (
              <div className="m-sheet-danger">
                {p.actions.filter((a) => a.danger).map((a) => (
                  <button key={a.label} className="ghost danger-outline" title={a.title} onClick={() => { setMenu(false); a.onClick?.(); }}>{a.label}</button>
                ))}
              </div>
            )}
            <p className="muted m-sheet-status">{p.connected ? "Live connection" : "Reconnecting…"} · {p.session}</p>
        </Dialog>
      )}
    </div>
  );
}
