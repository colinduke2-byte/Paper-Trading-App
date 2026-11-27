# Known limits

An honest list of what PaperDesk simulates, approximates, has not been tested for, and does not do.
Each item was checked against the code on the branch this was written for. If you rely on a result,
read the matching item first. The user guide ([USER_GUIDE.md](USER_GUIDE.md)) explains each topic.

Items marked **(code reading)** were read in the code but not run. Items marked **(tested)** were run
while writing this list.

## Never tested in the real world

- **A real phone.** The phone layout has only been tested with automated tests on emulated phones
  (Chromium with iPhone 13 and Pixel 7 profiles). Nobody has used it on a real phone, a real Wi-Fi
  network, or iOS Safari. Touch behaviour, the on-screen keyboard and pinch-zoom on charts are
  unverified.
- **The live data adapter with a real key.** At the time of writing, the Alpaca adapter has never been
  run with a real Alpaca key. It was built and tested against saved example responses and (for the
  start of the handshake) a fake key. Not verified: real quotes arriving, the 30-symbol streaming
  limit in practice, reconnecting after a real dropped connection, the asset list and option contracts
  (`paper-api.alpaca.markets`), whether the free plan includes corporate actions, news and delayed
  SIP data, and the units of Alpaca's option greeks (assumed per day for theta and per volatility point
  for vega). The `--check` command was run only without a key and with a fake key (tested).
- **Finnhub.** Not tested against the live service at all, including its free per-minute limit.
- **Windows.** `run.bat` is started by an automated Windows test, but nobody has used the Windows
  instructions by hand on a real PC while this guide was written. The firewall and Wi-Fi network
  steps are general Windows knowledge, not checked here, and Windows menus differ between versions.
- **Docker.** Built and started by an automated test; not hand-tested for this guide.
- **Plain HTTP on a real network.** LAN mode was tested on one machine (tested), not across a real
  home router.

## Simulated prices and data

- **The default market is fictional.** Prices follow a random walk with a fixed seed. Companies, their
  fundamentals, dividends and the yearly split are generated. Nothing about them predicts anything real.
- **Simulated prices only move while PaperDesk runs.** Orders can only fill while it runs, in both
  simulated and real-data mode.
- **"Always open" is the default in simulated mode.** The market never closes. A "day" is a UTC day, so a
  Day order expires and T+1 settlement happens at midnight UTC (7 pm New York time in winter, 8 pm in
  summer).
- **The depth ladder is always synthetic**, even with real data: it is built from the top-of-book quote.
- **Day P&L resets at midnight UTC in both session modes** (code reading), not at the market open, so with
  market hours it resets in the evening in New York.
- **Replay sessions that come with the program are simulated.** Sessions built from one-minute bars are
  approximate inside each minute; the order of prints and the quote around each is invented. Replay can
  only move forward.
- **The account value curve** is sampled about once a minute, only while PaperDesk runs. Volatility, Sharpe
  and beta need at least two daily returns.

## Fill model (see section 6 of the guide)

- No queue position, no hidden liquidity, no market impact beyond one cent per displayed-size level, no
  random slippage, no delay.
- **Market orders always fill in full**, however large, at a worse average price. A real market may not.
- A resting **limit order fills when the quote touches its price**, at the quote price (price
  improvement is possible), up to the displayed size per update.
- **Thin or missing quotes**: a missing bid or ask (common on IEX outside hours) is filled from the last
  trade price, so a market order on a thin stock can fill at the last trade price, better than a real
  fill would be.
- **Stops trigger on the last trade price**, then become market or limit orders. A stop is not
  guaranteed to fill near the stop price.
- **Auction orders** (market and limit on open or close) fill in full at the last trade price of the
  chosen quote, regardless of displayed size. They have no screen on this branch; in the engine only.
- **Multi-leg orders** are all-or-none at a net price taken from the legs' quotes (buying at the ask,
  selling at the bid). No screen on this branch.
- No trading halts, limit-up/limit-down, short-sale restrictions, locates or hard-to-borrow stocks. Any
  stock can be sold short.
- Fees: no stock commission and no regulatory fees; $0.65 per option contract on each fill, none on
  exercise or assignment.

## Options

- **Model prices**: Black-Scholes with a fixed 4% interest rate, no dividends, and an invented volatility
  skew. Open interest, volume and quote sizes are invented.
- **European-style pricing.** Real US stock options are American-style, so prices differ, mostly for deep
  in-the-money puts and calls before dividends.
- **No early assignment.** Exercise and assignment happen only at expiry (the close on the expiry date),
  automatically, for options in the money by at least one cent.
- **Indicative real quotes.** With the free Alpaca plan, option quotes are the "indicative" feed
  (delayed trades, modified quotes). Contracts the provider does not quote are priced by the model.
- **Splits**: only whole-number forward splits carry option positions over as new contracts; other splits
  cash-settle options at the model price.

## Accounts and rules

- Margin is a **Regulation T style simplification**: 50% initial, 25% long and 30% short maintenance,
  an uncovered-option formula, and strategy-aware requirements for spreads. No portfolio margin, no
  house or concentration requirements, no intraday margin.
- Margin interest (8%) and the borrow fee (0.5%) are fixed defaults that you can edit. They do not follow
  market rates.
- The pattern day trader rule and T+1 settlement are modelled from their usual descriptions, not from a
  broker's rulebook. In simulated mode a "day" is a UTC day.
- Dividends and splits are applied **only for the simulated feed**. With real data, the provider's
  dividends and splits appear in Research but are **not applied** to your accounts, and mergers,
  spin-offs, name changes and other corporate actions are not handled at all.
- **Wash-sale detail is informational**: it matches losses with replacement lots of the same symbol and shows
  the disallowed loss and the basis and holding period the replacement would carry, but it never changes
  realised gains or cost basis, does not look at similar securities or other accounts, and does not rescale
  carried amounts after a later split. Nothing in the reports is tax advice.
- There is no way in the screens to delete an account, deposit or withdraw cash.
- **A corrupt `state.json` is replaced silently** with two fresh default accounts (tested): PaperDesk does
  not keep a backup of the unreadable file. Back up `backend\data` yourself.

## Security and access

- **Phone access is plain HTTP.** The passcode and the sign-in cookie can be read by anyone watching
  the network. Home Wi-Fi only, and never reuse the passcode.
- One shared passcode for everyone. There are no user accounts. HTTPS is not included.
- A reverse proxy in front of PaperDesk is not supported.
- Browsers do not separate cookies by port, so the sign-in cookie is also sent to other web apps on the same
  address. Do not run untrusted web apps on the same computer in LAN mode.
- In Docker, every device shares one wrong-passcode counter.

## Settings that do not do what `.env.example` suggests

- `PAPERDESK_DATA` and `PAPERDESK_SEED` written in `.env` are **ignored** by the server (tested for
  `PAPERDESK_DATA`; code reading for `PAPERDESK_SEED`). Set them as environment variables instead.
  `PORT` in `.env` is used only by Docker, not by `run.bat` or `run.sh`.
- With real data on, if Alpaca cannot be reached when PaperDesk starts, it falls back to simulated
  prices *and to the simulated accounts folder*. The real-data accounts are not lost; they return after a
  restart in which Alpaca works (the banner says why).

## Reports

- The Reports screen says "All figures use simulated prices" and the statements say "All prices are
  simulated" even when real data is on (code reading). The numbers themselves use whatever prices the
  account used.
- The default benchmark is the fictional MKTX, which does not exist with real data. Type a real symbol
  such as SPY.

## Not built yet

- Screens for multi-leg orders, on-open and on-close orders, good-till-date orders,
  one-cancels-other groups and changing a working order. The engine supports them; another branch
  is building the ticket and strategy builder for them.
- Chart drawing tools, more indicators and keyboard shortcuts (another branch).
- Early assignment and American-style option pricing.
- User accounts and HTTPS.
- Real-provider corporate actions applied to accounts.
