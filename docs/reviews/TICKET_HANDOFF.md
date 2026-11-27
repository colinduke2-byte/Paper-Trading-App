# Hand-off to the ticket stream (stream/ticketui)

These items come from the UX review (`docs/reviews/UX_REVIEW_2026-11-24.md`). They are in files the ticket stream
owns: `frontend/src/Ticket.tsx` and the Orders/Positions row rendering in `frontend/src/Panels.tsx`, including the
Orders toolbar.

**stream/uxpolish does not edit these files.** It does provide the hooks listed in section A, so the ticket can
adopt them without depending on the rest of that branch's changes. Every hook is optional: if the ticket ignores
them, nothing breaks.

Read every item as: what is wrong, where it is, the exact change, and how to check it.

---

## A. What stream/uxpolish provides for the ticket to adopt

| Hook | Where | Use in the ticket |
|---|---|---|
| `useConnection()` → `{ connected: boolean, stale: boolean, since: number \| null }` | `frontend/src/connection.ts` (new). The provider is set in `App.tsx`. `stale` is true when the live stream has been down for more than 3 s. | Disable "Review" and "Place paper order" while `stale`, with `title="Waiting for the server to reconnect"`. Until then a CSS fallback in `styles.css` does it: `.app.stale .ticket .primary, .m-shell.stale .ticket .primary { pointer-events: none; opacity: .45 }`. Remove that rule once the ticket reads the hook. |
| `signedUsd(v)`, `usd(v)`, `price(v)`, `signed(v)`, `pct(v)` | `frontend/src/util.ts` | `usd`, `signed`, `pct`: negative zero now prints as zero. `signedUsd`: `+$1.20` / `−$3.40` / `$0.00`. `price(v)`: `$454.36`, with 4 decimals below $1. Use `usd`/`price` for every money value in the preview (problem 6). |
| `OPEN_SETTINGS` event name and `openSettings()` | `frontend/src/util.ts`; `App.tsx` listens for it. | The "Change this rule in Account rules" link in a rejection calls `openSettings()` (item B3). |
| `ticketFocus()` | `frontend/src/util.ts` | App calls this after "Close" on a position, after a chain click and after the welcome card's "Buy 10 MKTX". It scrolls `.ticket` into view (`block: "nearest"`) and focuses `.ticket input[data-ticket-qty]`. **Add `data-ticket-qty` to the Shares/Contracts input** so the focus lands on it. |
| `.ticket-actions` sticky row on phones | `frontend/src/mobile.css` | Wrap the bottom action row (Review, or Edit + Place) in `<div className="ticket-actions">`. While the on-screen keyboard is up (`.m-kb`), mobile.css makes it `position: sticky; bottom: 0` with the panel background. Put the one-line cost summary inside it (B1). |
| `<Info term="…"/>` popover | `frontend/src/Info.tsx` (new); definitions in `frontend/src/glossary.ts` | It is tap-friendly. Put it after the labels "Time in force", "Buying power after", "Estimated price" and "Take profit and stop loss". Terms available: `buying_power`, `buying_power_options`, `maintenance_margin`, `settled_cash`, `day_trades`, `delta`, `iv`, `oi`, `twr`, `sharpe`, `max_drawdown`, `wash_sale`, `mark`, `time_in_force`, `bracket`, `limit_order`, `stop_order`. |
| `.danger` button style | `frontend/src/styles.css` | Use for destructive confirmations (for example "Cancel all orders" in a confirm dialog). |

---

## B. Ticket.tsx

**B1. The preview must show cost and risk before placing (review problem 6).**
Current location: `Ticket.tsx`, the preview block (`preview && preview.ok`).

Change every money value to `usd()`/`price()`. Then add rows depending on the order:

- **Options** (`isOption(seed.symbol)`). Replace "Estimated price 75.40" with two rows:
  - `Price per share  $75.40`
  - `Contract size  100 shares per contract`
  - Keep `Estimated cost  $7,540.65`.
  - Option buys: add `Most you can lose  $7,540.65 (the premium)`.
- **Short options** (sell to open). Read `preview.buying_power_before - preview.buying_power_after`. For a cash account, show `Cash set aside to secure this put  $X`; for a margin account, `Margin required  $X`. Add the warning line: `If the option is assigned you must buy (put) or deliver (call) 100 shares per contract.`
- **Limit that cannot fill now.** Buy with limit < ask, or sell with limit > bid. Add the line: `Fills only at $400.00 or better. The current ask is $454.65, so this order will wait in the Orders tab.` Change the "Position after" label to `Position after it fills`.
- **Bracket attached.** Entry ≈ `preview.estimated_price`; n = qty. Add:
  - `Take profit at $500.00  about +$228.05` (computed (tp − entry) × n for a buy, mirrored for a sell)
  - `Stop loss at $420.00  about −$171.95`
- **Sell more than the position held** (position.qty > 0 and qty > position.qty):
  - Side label: `Sell 15 and short 85`.
  - Preview warning: `This sells your 15 shares and opens a short position of 85 shares.`
  - Mirror for buy-to-cover larger than the short.
- **Phone with the keyboard open:** put the action row in `.ticket-actions` (section A). Inside it, show a one-line summary above the buttons: `Est. cost $1,361.55` (or `Est. proceeds …`).

Check: e2e that previews an option buy and asserts `Contract size` is visible, and a limit 10% below the ask asserts `will wait`.

**B2. Rejection text (W3, review problem 8).**
- Current: `setError(\`This order would be rejected: ${p.reason}.\`)`. It doubles the period when the reason already ends in "." (the PDT and settled-cash reasons do).
- Change to: a bold heading `This order can't be placed`, then the reason with its first letter capitalised and exactly one final period, e.g. `reason.replace(/^./, c => c.toUpperCase()).replace(/[.\s]*$/, ".")`.
- Check: e2e with a PDT-blocked account asserts the text does not contain `..`.

**B3. Link from a rejection to the rule.**
- When the reason starts with `Pattern day trader` or `Cash accounts can only buy with settled cash`, add a link-style button under it: `Change this rule in Account rules`. Its onClick calls `openSettings()` from util.ts.
- When stream/fixes lands its buying-power messages that name the controlling setting, match on those too.

**B4. Cash account with no shares, side Sell (W66).**
- Current: `sideLabel` gives `Sell short`, and the order is then rejected.
- When the account type is `cash` (pass `accountType` from App: `accounts.find(a => a.id === accountId)?.type`) and there is no long position:
  - Keep the side label `Sell`.
  - Show a muted hint under the side toggle: `You have no MKTX shares to sell. Cash accounts cannot sell short.`
  - Disable Review with that text as its title.

**B5. Messages after placing (W23, W24).**
- Filled:
  - Current: `Filled: ${o.side} …` → shows `Filled: buy 10 MKTX at 454.36`.
  - New: `Bought 10 MKTX at $454.36` / `Sold 10 MKTX at $454.36` (options: `Bought 1 MKTX 04 Dec 2026 380 Call at $75.40`).
- Placed, not filled:
  - Current: `Order placed: buy 5 MKTX (working)`.
  - New: `Order placed: buy 5 MKTX, limit $400.00. It fills if the price reaches your limit; see the Orders tab.` For stops: `…stop $420.00. It becomes a market order if the price reaches the stop; see the Orders tab.`

**B6. Inline confirmation in the ticket.**
- After a successful place, keep a small `.notice` (not `.bad`) in the ticket with the B5 text plus a link button `View in Positions` (filled) or `View in Orders` (working).
- The link sets the bottom tab. App will expose `onShowTab(tab)` if you need it; or dispatch `new CustomEvent("pd:show-tab", { detail: "Orders" })`, which App/Panels listen for on stream/uxpolish.
- Clear it on the next edit (inside `edit()`).

**B7. Reset bracket fields after placing.**
- After a successful `place()`, call `setBracket(false); setTp(""); setSl("")`, as well as clearing qty.
- Keep the order type and the limit.
- Reason: today a second order (or a side switch) silently reuses the old take-profit and stop-loss and is rejected.

**B8. Placeholders and empty quote (W81, W82).**
- `No quote` → `No price yet for this symbol.`
- The Shares placeholder `0` → `e.g. 10`; contracts `e.g. 1`.

**B9. Disabled Review without a reason.**
- `disabled={busy || !qty}` → add `title={!qty ? "Enter a number of shares first" : undefined}`.
- Also disable it while `useConnection().stale` (section A).

**B10. Focus after placing.**
- After a successful place, focus the qty input (`data-ticket-qty`). Today focus stays on a button that re-renders as Review.

**B11. "Max" quantity button.**
- A small ghost button inside the qty label row.
- Buy stock: `Math.floor(summary.buying_power / ask)` (cash accounts: `settled_cash` when the settled-cash rule is on).
- Buy option: `Math.floor(summary.option_buying_power / (ask * 100))`.
- Sell: `Math.abs(position.qty)`.
- Needs `summary` passed from App (already available there as `summary`).
- Title: `Largest quantity your buying power allows at the current ask`.

**B12. Dollar-amount entry.**
- A two-button segment above the qty input: `Shares | $ amount`. Stocks only, market and limit with time in force Day; the backend allows 4-decimal fractional shares for exactly these.
- In `$ amount` mode, the input is dollars. Send `qty = floor((amount / ask) * 10000) / 10000`.
- Show under it: `≈ 2.1999 shares at $454.65`.

**B13. Position-sizing hint.**
- Under the qty input, muted: `10 shares ≈ $4,546 · 4.6% of account value`, using ask (buy) or bid (sell) and `summary.equity`.

**B14. Order type help.**
- Put `<Info term="time_in_force"/>` after "Time in force".
- Add a one-line muted description under the order-type select:
  - Market: "Fills now at the best available price."
  - Limit: "Fills only at your price or better."
  - Stop: "Becomes a market order when the price reaches the stop."
  - Stop limit: "Becomes a limit order when the price reaches the stop."
  - Trailing stop: "A stop that follows the price up (or down for a short) by a fixed amount or percent."

---

## C. Panels.tsx: Orders and Positions (owned by the ticket stream)

**C1. Cancel all needs confirmation (review problem 10).**
- `cancelAll` (`Panels.tsx`, Orders toolbar): before the DELETE, `if (!window.confirm(\`Cancel all ${n} open orders? Stop-loss and take-profit orders that protect your positions are included.\`)) return;`
- Add a `busy` state so the button cannot fire twice.

**C2. Cancelling one bracket leg.**
- Where the row's Cancel is clicked, if `o.role === "stop_loss"` or `"take_profit"`, confirm first: `Cancel this stop loss? Your 5 MKTX shares will no longer be protected by it.` (take profit: `…will no longer be sold automatically at $500.00.`).

**C3. Cancel toast (W22).**
- `Order canceled` → `Canceled: buy 5 MKTX limit $400.00` (side, qty, description, price text from `orderPrice`).

**C4. Price column (W64).**
- `orderPrice()`: `limit 400.00` → `Limit $400.00`, `stop 420.00` → `Stop $420.00`, `market` → `Market`, trailing → `Trail $2.00 (stop $452.10)` / `Trail 1.5%`.

**C5. Type column (W65).**
- `Stop (stop loss)` → `Stop loss`; `Limit (take profit)` → `Take profit`.
- An entry order that has children: `… (with take profit and stop loss)`, or visually group the children under it, indented.
- Liquidation orders (`role === "liquidation"`): `Margin call sale`.

**C6. Column naming (terminology table).**
- Orders `Duration` → `Time in force`.
- Positions `Open P&L` / `Open %` → `Unrealized P&L` / `Unrealized %`.
- Positions `Mark` → `Last`. Or keep `Mark` with `<Info term="mark"/>`.
- Use `signedUsd()` for the P&L columns and `usd()` for `Avg cost`/`Last`.

**C7. Close button (W63).**
- `Close…` → `Close`, with `title="Fill the order ticket to close this position"`.
- App's `closePosition` already calls `ticketFocus()` on stream/uxpolish.

**C8. Position Day P&L for positions opened today.**
- Today Day P&L is measured from the previous close even for shares bought today: +$390,000 on a position bought minutes earlier, while the header showed −$67,680.
- stream/fixes owns "the trading-day definition for Day P&L". Once it lands, check that the row uses the new field.

**C9. Phone cards for Positions and Orders (review problem 7).**
- Below 600 px wide (`useSmallScreen()` plus `matchMedia("(max-width: 599px)")`), render a list of cards instead of the table.
- Position card:
  - Line 1 bold: description, plus the qty tag.
  - Line 2 muted: `15 sh · avg $454.37 · last $453.88`.
  - Line 3: `Unrealized −$7.60 (−0.11%)` toned, then `Today +$26.40`.
  - A full-width 44 px `Close` button.
- Order card:
  - Line 1: `Buy 5 MKTX`.
  - Line 2: `Limit $400.00 · Day · Working`.
  - Line 3 muted: created time.
  - A full-width `Cancel` button.
- Check: the e2e phone project asserts the Close button is visible without horizontal scrolling at 390 px.

**C10. Orders empty state.**
- `No open orders.` → `No open orders. Orders that wait for a price (limit and stop orders) appear here.`

**C11. Positions empty state on the phone (W34).**
- When on the phone shell: `No open positions. Tap Trade below to place a paper trade.`

**C12. "Show finished orders" wraps onto three lines at 390 px.**
- Give the label `white-space: nowrap`, or shorten it to `Show finished`.

---

## D. Option chain integration

stream/uxpolish makes a small edit in `Market.tsx` `OptionChain`:
- It scrolls the at-the-money row into view on load and when the expiry changes.
- It inserts a spot divider row (`<tr className="spot-row">`).
- It strengthens the in-the-money shading (CSS only).
- It disables the bid/ask buttons when the price is 0.

The `cells()`/`onTrade` signature is unchanged. If you add "add to strategy" controls to the chain, keep the extra `tr.spot-row` in mind when indexing rows.
