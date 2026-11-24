import { ReactNode, useEffect, useRef } from "react";
import "./help.css";

// In-app help. The app runs offline, so the essentials are here; the full guide is docs/USER_GUIDE.md in the
// PaperDesk folder. Everything below describes what the screens do today: keep it in step with the UI.

type Section = { id: string; title: string; body: ReactNode };

const SECTIONS: Section[] = [
  {
    id: "start",
    title: "Getting started",
    body: (
      <ol>
        <li>Pick an account in the account menu. The first start creates a margin account ($100,000) and a cash account ($25,000). On a phone, the menu is the button at the top right.</li>
        <li>Choose a symbol from the watchlist, or search for one by symbol or company name.</li>
        <li>Open the order ticket (on a phone, press Buy or Sell, or the Trade tab). Enter the number of shares and press Review.</li>
        <li>Check the estimate, then press Place paper order. Nothing is sent anywhere: PaperDesk only simulates.</li>
        <li>Follow the order under Orders, Fills and Positions. Reports has gains, statements and a journal.</li>
      </ol>
    ),
  },
  {
    id: "banner",
    title: "What the banner means",
    body: (
      <>
        <p>The strip at the top always says <b>Paper trading · no real money</b>. The rest of it names where prices come from:</p>
        <ul>
          <li><b>simulated market data</b>: the companies are fictional and the prices are generated. This is the default.</li>
          <li><b>real-time … prices via Alpaca</b>: real quotes from your own Alpaca key. Orders are still only simulated.</li>
          <li><b>data delayed</b> or <b>prices may be stale</b>: the feed is late or interrupted. Do not trust the prices.</li>
          <li>A blue banner, <b>Replay of …</b>: you are in a replay sandbox, not on your normal accounts. Press Exit replay to leave.</li>
        </ul>
        <p>Hover over the banner on a computer for more detail. Option prices marked <i>model prices</i> are calculated, not quoted.</p>
      </>
    ),
  },
  {
    id: "orders",
    title: "Order types",
    body: (
      <>
        <ul>
          <li><b>Market</b>: buy or sell now at the best available price. Large orders walk through the book and can cost more than the estimate.</li>
          <li><b>Limit</b>: buy at your price or lower, or sell at your price or higher. It may fill partly, or not at all.</li>
          <li><b>Stop</b>: waits until the last price reaches the stop price, then becomes a market order.</li>
          <li><b>Stop limit</b>: waits for the stop price, then becomes a limit order, so it may not fill.</li>
          <li><b>Trailing stop</b>: a stop that follows the price by a dollar amount or a percentage, and only moves in your favour.</li>
        </ul>
        <p><b>Time in force</b>: Day (ends at the close), Good till canceled, Immediate or cancel (fill what you can now, cancel the rest) and Fill or kill (all of it now or none). Immediate and fill or kill are for market and limit orders only.</p>
        <p><b>Attach take profit and stop loss</b> adds two exit orders that start when the entry order fills; when one fills the other is canceled. <b>Allow extended hours</b> lets a limit order trade before and after the regular session.</p>
        <p>Review shows an estimate and any warning. If the order would be rejected, it says why.</p>
      </>
    ),
  },
  {
    id: "accounts",
    title: "Accounts, margin and short selling",
    body: (
      <>
        <p>A <b>cash account</b> cannot borrow and cannot sell short. A <b>margin account</b> can borrow against its holdings and can sell short (Sell more shares than you own). Buying power on a margin account is about twice its spare equity for stocks.</p>
        <p>If a margin account's value falls below the maintenance requirement you get a <b>margin call</b>. Unless you fix it within the grace period, PaperDesk sells positions for you (you can turn that off in Settings). The grace period counts only time while the market is open and PaperDesk is running: time while the app is closed or a replay runs does not count, and after such a pause the account log says how much grace is left. Account value includes dividends pending and subtracts margin interest and borrow fees accrued but not yet charged. The <b>Settings</b> button lists every account rule (settlement, day trading limit, interest, margin calls) with its default.</p>
        <p><b>Reset</b> erases an account's history and starts again. It cannot be undone.</p>
      </>
    ),
  },
  {
    id: "options",
    title: "Options",
    body: (
      <>
        <p>Open the Options tab. Choose an expiry, then press an <b>ask</b> price to buy or a <b>bid</b> price to sell. One contract covers 100 shares. Calls are on the left and puts on the right.</p>
        <p>Without a market data key the prices are <b>model prices</b>, calculated from the share price. They are not quotes. Contracts that finish in the money are exercised or assigned automatically at the close on the expiry date.</p>
      </>
    ),
  },
  {
    id: "phone",
    title: "Using it on a phone",
    body: (
      <>
        <p>Phone access is off by default. To turn it on, set a passcode and LAN mode in the <code>.env</code> file, restart PaperDesk, and open the address it prints from a phone on the same Wi-Fi. Home Wi-Fi only: the connection is not encrypted.</p>
        <p>On a phone, the tabs at the bottom switch between Watchlist, Chart, Trade and Account. The menu at the top right holds the account picker, New account, Reset, Delete account, Replay, Settings, this help and Sign out.</p>
      </>
    ),
  },
  {
    id: "about",
    title: "About",
    body: (
      <>
        <p>PaperDesk is a paper trading simulator. It uses open-source software; the file <code>THIRD_PARTY_NOTICES.md</code> in the PaperDesk folder lists every package with its licence.</p>
        <p>Price charts: <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">Charting by TradingView</a> (TradingView Lightweight Charts, Copyright TradingView, Inc., Apache License 2.0).</p>
      </>
    ),
  },
  {
    id: "more",
    title: "Where to read more",
    body: (
      <p>The full guide is the file <code>docs/USER_GUIDE.md</code> in the PaperDesk folder, with <code>docs/QUICK_START.md</code> for a one-page start and <code>docs/KNOWN_LIMITS.md</code> for what is simulated or approximate. PaperDesk is a simulation for practice. Nothing in it is investment advice.</p>
    ),
  },
];

export function HelpDialog({ onClose }: { onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal help-modal" role="dialog" aria-modal="true" aria-labelledby="help-title" onClick={(e) => e.stopPropagation()}>
        <div className="help-head">
          <h3 id="help-title">Help</h3>
          <button ref={close} className="ghost" onClick={onClose}>Close help</button>
        </div>
        <p className="muted help-lead">PaperDesk simulates trading with pretend money. It never sends an order to a broker.</p>
        <div className="help-body">
          {SECTIONS.map((s, i) => (
            <details key={s.id} className="help-sec" open={i === 0}>
              <summary>{s.title}</summary>
              <div className="help-text">{s.body}</div>
            </details>
          ))}
        </div>
      </div>
    </div>
  );
}
