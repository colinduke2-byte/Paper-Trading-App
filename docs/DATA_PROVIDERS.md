# Market data providers

PaperDesk runs on simulated prices for fictional companies until you give it a data key.
With a free Alpaca key it switches to real US stocks, ETFs and listed options. An optional
free Finnhub key adds earnings dates and company fundamentals.

Facts below were read from the providers' official pages on 2026-11-24. Each one names the
page it came from. Anything not confirmed on an official page is marked **UNVERIFIED**.
Nothing was tested with a real key, because none was available. See "What has not been
tested against the live service" at the end.

## Decision

**Primary: Alpaca, free Basic plan. Secondary: Finnhub, free plan.**

| Need | Alpaca Basic (free) | Finnhub free | Why the others were not chosen |
| --- | --- | --- | --- |
| Real-time stock quotes | Yes, IEX only (one exchange, about 2.5% of volume), REST and websocket | REST quote, websocket trades | Massive (formerly Polygon) Basic: end-of-day data only, 5 calls/min, no websocket ([pricing](https://massive.com/pricing)). Alpha Vantage free: 25 requests per day ([premium](https://www.alphavantage.co/premium/)). |
| Streaming | 30 symbols, 1 connection | not used | |
| History for charts | Since 2016, 200 calls/min | candles are premium | |
| Options | Indicative feed: chain, quotes, greeks, implied volatility | none | |
| News | Yes (`/v1beta1/news`) | company news, 1 year | |
| Corporate actions | Yes (`/v1/corporate-actions`) | dividends are premium | |
| Earnings calendar, fundamentals | no | Yes | |
| Licence for personal display | Data plan included with a free paper account | personal use only, no redistribution | Twelve Data's terms licence data for "Internal Use" only and define display separately ([terms](https://twelvedata.com/terms)). Tiingo's pricing page did not load without JavaScript, so it was not checked (**UNVERIFIED**). |

The tentative earlier choice was right. One detail changed: option prices come from REST
snapshots, not the websocket, because Alpaca's option stream only speaks msgpack.

## Alpaca: verified facts

| Fact | Value | Source |
| --- | --- | --- |
| Plans | Basic is free and is the default for paper and live accounts. Algo Trader Plus costs $99 a month. | [about-market-data-api](https://docs.alpaca.markets/us/docs/about-market-data-api.md) |
| Real-time stocks on Basic | IEX exchange only. SIP (all exchanges) needs Algo Trader Plus. | same page; [market-data-faq](https://docs.alpaca.markets/us/docs/market-data-faq.md) |
| Stream symbol limit | 30 symbols on Basic, unlimited on Algo Trader Plus | about-market-data-api |
| Stream connections | "in many subscriptions (or without one) this limit is 1"; a second connection gets error 406 | [streaming-market-data](https://docs.alpaca.markets/us/docs/streaming-market-data.md) |
| REST limit | 200 calls per minute on Basic. Over the limit: HTTP 429 with `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (UNIX time) headers. | about-market-data-api; the OpenAPI block on [stockbars](https://docs.alpaca.markets/us/reference/stockbars.md) |
| History depth | Since 2016 | about-market-data-api |
| Latest 15 minutes | Restricted on Basic for SIP data. IEX is "the only feed that can be used without a subscription" in real time. Historical SIP queries are allowed when `end` is at least 15 minutes old. | about-market-data-api; market-data-faq; [historical-stock-data-1](https://docs.alpaca.markets/us/docs/historical-stock-data-1.md) |
| 15-minute delayed SIP | `delayed_sip` is a documented feed value for latest and snapshot endpoints and the stream (`v2/delayed_sip`). Whether Basic includes it is **UNVERIFIED**; the plan table does not list it. | stocksnapshots reference; [real-time-stock-pricing-data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md) |
| Options on Basic | "Indicative Pricing Feed", 200 option quote subscriptions. The indicative feed is "a free indicative feed where trades are delayed and quotes are modified". | about-market-data-api; [optionchain](https://docs.alpaca.markets/us/reference/optionchain.md) |
| Option stream format | msgpack only (error 412 otherwise), no `*` wildcard for quotes | [real-time-option-data](https://docs.alpaca.markets/us/docs/real-time-option-data.md) |
| Option chain endpoint | `GET /v1beta1/options/snapshots/{underlying}`: latest quote, latest trade, greeks and implied volatility per contract; filters for expiry and strike; up to 1000 per page | optionchain |
| Listed contracts and open interest | `GET /v2/options/contracts` on the trading host (read-only). Only active contracts by default; `expiration_date_lte` defaults to the next weekend, so PaperDesk always sends it; `limit` up to 10000 per page. Each contract has `root_symbol`, `size` (shares delivered) and `multiplier`; adjusted (non-standard) contracts have another root or size. | [get-options-contracts](https://docs.alpaca.markets/us/reference/get-options-contracts.md) (updated 2026-05-27) |
| News | `GET /v1beta1/news?symbols=` | [news-3](https://docs.alpaca.markets/us/reference/news-3.md) |
| Corporate actions | `GET /v1/corporate-actions`: dividends, splits, spin-offs, mergers, name changes and more. Whether Basic restricts it is not stated (**UNVERIFIED**, assumed free). `start`/`end` are described as the interval of actions "sorted by their `process_date`" (default today); `limit` 1 to 1000 (default 100) counts records, not symbols; `data_quality=complete` (default) hides incomplete records not yet processed; the page warns that Alpaca "has no guarantees on the creation time of corporate actions" and records may appear late. Each record kind has a documented example (saved verbatim as a fixture). | [corporateactions-1](https://docs.alpaca.markets/us/reference/corporateactions-1.md) (updated 2026-05-27) |
| Calendars | Market calendar and clock only. No earnings calendar. | llms.txt index |
| Fundamentals | None | llms.txt index |
| Symbol list | `GET /v2/assets` on the trading host | [get-v2-assets-1](https://docs.alpaca.markets/us/reference/get-v2-assets-1.md) |
| Sign-up | "Anyone globally can create an Alpaca Paper Only Account! All you need to do is sign up with your email address." A paper-only account is "only entitled to receive and make use of IEX market data". | [paper-trading](https://docs.alpaca.markets/us/docs/paper-trading.md) |
| Keys | Dashboard, "API Keys" on the right sidebar, "Generate New Keys". Resetting a paper account requires new keys. | [getting-started](https://docs.alpaca.markets/us/docs/getting-started-with-alpaca-market-data.md); market-data-faq |
| REST auth | Headers `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`. HTTP Basic is for Broker API partners, not personal keys. | about-market-data-api; market-data-faq |
| Stream auth | After `[{"T":"success","msg":"connected"}]` send `{"action":"auth","key":"…","secret":"…"}` within 10 seconds (headers also work). | streaming-market-data |
| Hosts | REST data: `data.alpaca.markets`. Stream: `stream.data.alpaca.markets` (`/v2/iex`, `/v2/sip`, `/v2/delayed_sip`, `/v1beta1/indicative`, `/v2/test`). Assets and option contracts: `paper-api.alpaca.markets` for paper keys, `api.alpaca.markets` for live keys. | same pages |
| Stock quote sizes | "ask size in round lots" (PaperDesk multiplies by 100) | real-time-stock-pricing-data |
| Licence | Personal use of the data under your own account. Alpaca's market data agreement was not read in full (**UNVERIFIED**). PaperDesk keeps the data on your own machine and does not redistribute it. | — |

Checked against the real service without a key (2026-11-24): `data.alpaca.markets` answers
HTTP 401 without credentials. The stream at `stream.data.alpaca.markets/v2/iex` accepted a
websocket connection through the proxy, sent the `connected` greeting, and answered a dummy
key with error 402. PaperDesk stopped and showed "Alpaca rejected the API key". The
trading host `paper-api.alpaca.markets` was not reachable from the build environment.

## Finnhub: verified facts

Source: [finnhub.io/docs/api](https://finnhub.io/docs/api) (the endpoint data embedded in the page) and [terms of service](https://finnhub.io/terms-of-service).

- Base `https://finnhub.io/api/v1`. Authentication with the `token=` query parameter or the
  `X-Finnhub-Token` header. PaperDesk uses the header, so the key never appears in a URL.
- Over the limit: HTTP 429. There is also a cap of 30 calls per second on every plan. The free
  per-minute limit is on the pricing page, which needs JavaScript (**UNVERIFIED**; often quoted
  as 60 per minute). PaperDesk budgets 50 per minute and caches results for hours.
- Free endpoints used: `/calendar/earnings` (free tier: "1 month of historical earnings and new
  updates"), `/stock/profile2`, `/stock/metric`, and `/company-news` (free tier: "1 year of
  historical news"). `/stock/dividend` and `/stock/candle` are marked "Premium Access Required"
  and are not used.
- Licence: "All plan listed on Finnhub website is strictly for personal use"; no
  redistribution without written approval. Not for business use or securities professionals.

## Sign-up and setup

### Alpaca (required for real data)

1. Go to <https://alpaca.markets> and choose **Sign up**. Use your email address. A paper-only
   account is enough. No funding or identity documents are needed for paper only.
2. Confirm your email and sign in to the dashboard at <https://app.alpaca.markets>.
3. Make sure the account switcher at the top left shows your **Paper** account.
4. On the right sidebar under **API Keys**, choose **Generate New Keys**. Copy the key ID
   (starts with `PK`) and the secret. The secret is shown only once.
5. Put both in a file named `.env` in the PaperDesk project folder (next to `run.sh`):

   ```
   APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxxxx
   APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

6. Restart PaperDesk. The banner should read "real-time IEX prices via Alpaca".
7. Optional check from `backend/`: `.venv/bin/python -m app.marketdata.alpaca --check` (on Windows
   `.venv\Scripts\python -m app.marketdata.alpaca --check`). It prints one PASS or FAIL line each
   for the symbol list, a quote, daily bars, an option chain, the option contract listing, a
   corporate actions query, a snapshot of a thinly traded symbol, building a small replay session
   and the stream. It never prints keys. See "First run with real keys" below.

If you reset the paper account in Alpaca's dashboard, generate new keys and update `.env`.
Only one program can use the stream for each key. If another tool is connected, PaperDesk
shows "connection limit exceeded" and falls back to polling.

### Finnhub (optional: earnings dates and fundamentals)

1. Go to <https://finnhub.io/register>, create a free account and confirm your email.
2. Copy the API key from the dashboard.
3. Add `FINNHUB_API_KEY=…` to the same `.env` file and restart.

### Where keys live

- `.env` in the project root is read at start-up by PaperDesk itself (in `default_exchange`), so
  it works with `run.sh`, `run.bat`, Docker or a plain `uvicorn` command. Variables already set
  in the environment take priority over the file. Paths with spaces and Windows backslashes are
  fine. The file is read as UTF-8 and a byte order mark is ignored.
- Never commit `.env`. Keys are sent only in request headers. They are never logged, never put in
  URLs and never shown in the UI or in `/api/status`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAPERDESK_FEED` | `auto` | `auto`: Alpaca when both keys are set, otherwise synthetic. `alpaca` or `synthetic` to force one. |
| `APCA_API_KEY_ID` | — | Alpaca key ID (Alpaca's own variable name, as in its SDKs). |
| `APCA_API_SECRET_KEY` | — | Alpaca secret key. |
| `APCA_API_BASE_URL` | `https://paper-api.alpaca.markets` | Trading host, used only for the symbol list and option contracts. Use `https://api.alpaca.markets` with live-account keys. |
| `PAPERDESK_ALPACA_FEED` | `iex` | `iex` (free, real time), `sip` (paid), `delayed_sip` (15 minutes late; free availability UNVERIFIED). |
| `PAPERDESK_ALPACA_OPTIONS_FEED` | `indicative` | `indicative` (free) or `opra` (paid). |
| `PAPERDESK_ALPACA_RPM` | `190` | REST request budget per minute (the free plan allows 200). |
| `FINNHUB_API_KEY` | — | Optional. Earnings calendar and fundamentals. |
| `PAPERDESK_SESSIONS` | `us` with Alpaca, `always` with synthetic | `us` follows real market hours. |
| `PAPERDESK_DATA` | `./data` | With Alpaca, state goes to the `alpaca` subfolder, so accounts holding fictional symbols never mix with real ones. |

When no key is set, `/api/status` keeps `feed: "synthetic"` and gives the cause in
`fallback_reason`. A rejected key (HTTP 401 or 403) or an unreachable Alpaca at start-up also
falls back to synthetic, with the cause shown.

## How the Alpaca feed works

- **No blocking in the tick loop.** Two daemon threads do all network I/O: the stream thread
  (the `websockets` threaded client, which works the same on Windows, macOS and Linux) and a
  poller. They write into a lock-protected latest-quote map. `step(now)` only drains that map.
- **Choosing what to stream.** Symbols in priority order are: open orders, positions, what
  open browser tabs show, then watchlists. The first 30 are streamed (trades and quotes). The
  rest are polled with `/v2/stocks/snapshots` in batches of 100 every 5 seconds (every 60 seconds
  while the market is closed). Streamed symbols also get a snapshot every 60 seconds to refresh
  open, high, low, volume and previous close.
- **Stream errors.** 402 (bad key) and 409 (plan does not cover the feed) stop the stream and
  explain the cause. They do not retry, so a bad key is not sent again and again. 406
  (connection in use) waits 60 seconds. 405 (symbol limit) reduces the cap to what the server
  accepted, or halves it, and polls the rest. A dropped connection reconnects with exponential
  backoff (1 to 60 seconds, with jitter), then authenticates and subscribes again.
- **Staleness.** `/api/status` sets `stale: true` during market hours when the stream has been
  silent for 2 minutes, or when it is down and REST has not succeeded for 90 seconds. The banner
  then says prices may be stale.
- **Quote age.** Every quote carries `stale` (in `Quote`, `/api/quotes`, the websocket ticks and
  position rows). It is worked out each time the quote is read, from the provider's own time of the
  newest quote or trade for the symbol: stale when that is more than **120 seconds** old in the
  regular session, more than **15 minutes** old before the open or after the close (4:00 to 9:30
  and 16:00 to 20:00 New York time), and, while the market is closed, when it is from before the
  open of the latest session. A stale quote is never traded against: a new market order during the
  regular session is refused with the cause, other orders are accepted but rest, the tick does not
  match resting orders against it, a model option price from a stale underlying is stale too, and
  margin call liquidation does not use it. It still values positions (better than no value):
  the account summary lists such symbols in `stale_prices`, and `prices_unavailable` stays for
  symbols with no price at all. The simulated feed never sets `stale`.
- **Missing prices** (IEX is one exchange, so a book is often one-sided or empty). The rules are
  deliberate and conservative:
  - A missing (or crossed) side is filled from the last trade **only while that trade is at most 60
    seconds old**. So a market order on a thin symbol can fill at a trade price from the last minute,
    never at an old one. After 60 seconds the side is shown empty (0) again.
  - With an empty side, a new market order during the regular session is rejected with the cause
    ("No ask for X right now, so a market buy has no price to fill at. Use a limit order, or wait
    for an ask."). Outside the session it is accepted and waits. A resting market or triggered stop
    order never fills against an empty side: it waits (this is enforced in the matching engine for
    every feed). Limit orders rest as usual.
  - Marks (position value, margin) use the last trade; before any trade the quote midpoint; with no
    two-sided quote either, the previous close. Such prices carry `stale: true` and a `note` in
    `/api/quotes`, and positions carry `price_note`, so a price from an earlier session is labelled.
  - `prices_unavailable` (margin checks paused) engages only when there is no price at all: no trade,
    quote or previous close for a stock, and for an option neither a provider quote nor an
    underlying price for the model. An option quote with no bid or ask is marked at its last trade.
  - Unknown symbols are rejected as unknown; a listed symbol with no data yet says "No quote has
    arrived from Alpaca for X yet. It may be halted, not trading on IEX yet today, or the first
    request is still in progress."
  - An option that expires while its underlying has no price waits (exercise is not guessed from
    a zero price) and is settled on the first tick with a price; an event says so.
- **Rate limiting.** A token bucket shared by all threads (190 per minute by default). On 429
  it blocks until `X-RateLimit-Reset`. A web request waits at most 5 to 10 seconds, then shows
  cached data or an error. 5xx responses and network errors are retried twice with backoff.
- **Charts.** Every UI timeframe maps to Alpaca bars: 1m→`1Min`, 5m→`5Min`, 15m→`15Min`,
  1h→`1Hour`, 1d→`1Day`, 1w→`1Week`. Bars are fetched newest first with `sort=desc` and cached for
  30 seconds (1m) up to 1 hour (1w). Intraday bars use IEX. Daily and weekly bars use
  consolidated (SIP) history ending 16 minutes ago, which is free and has full volume. If that is
  refused, they use IEX. Every timeframe is requested split-adjusted (`adjustment=split`), so a
  chart that spans a split has no jump.
- **Options.** The listed contracts come from `/v2/options/contracts` (active, the next 800 days,
  cached for an hour; a failed refresh keeps the last list). Only standard contracts are used: root
  equal to the underlying, 100 shares, multiplier 100; adjusted contracts are left out because the
  simulator only knows 100-share deliverables. That listing, not the model calendar, drives order
  validation (single orders and every multi-leg leg), the expirations route, and the chain's
  strikes: an expiry the provider lists is tradable even when it is not a Friday, and an expiry or
  strike it does not list is rejected ("expiry is not listed: Alpaca lists no SPY options expiring
  …", "strike is not listed: …"). The chain shows every listed strike within ±30% of spot; legs the
  option-chain snapshot quotes are marked with the feed, the rest `source: "model"`. If no option
  quote arrives at all, the chain is still built on the listed strikes, labelled "model prices on
  the strikes Alpaca lists … not market data". Option orders fill against the provider quote when it
  is less than 15 minutes old, and against the model otherwise. Contracts you hold or have orders on
  are polled every 5 seconds. Expiry and exercise use the contract's own date (from its OCC symbol,
  which is the provider's expiration date) and that day's real close (early closes included).
  A held contract the provider no longer lists (expired, delisted, or adjusted after a split) can
  still be closed; it is valued by the option model and its position row says so (`price_source:
  "model"`, `price_note`). If the listing cannot be loaded at all, the model calendar applies.
- **Corporate actions** (dividends and splits applied to accounts). See the section below.
- **Symbol list.** Active, tradable US equities except OTC, cached for a day in
  `alpaca_assets.json` in the data folder. Alpaca has no ETF flag, so names containing "ETF" are
  shown as funds. `/api/symbols` returns at most `limit` rows (default 50, at most 500): with an
  empty query, the watchlist, held and open-order symbols first, then the rest alphabetically; with a
  query, every symbol is searched (exact symbol, then prefix, then contains, then company name).
  `/api/symbols/lookup?symbols=A,B` returns the ones the active feed knows. The UI uses it at start
  and on a live/replay switch, so a browser that last showed a fictional symbol (remembered in
  localStorage) moves to the first watchlist symbol before anything is requested for it.
- **Never trades.** The HTTP layer implements only GET. No order endpoint exists in the code, and a
  test checks for that.

## Corporate actions applied to accounts

The lifecycle package (`backend/app/lifecycle/`) pays dividends and applies splits. With Alpaca
these come from `/v1/corporate-actions` (code: `backend/app/marketdata/alpaca_actions.py`).

- **Which symbols.** Only stocks the accounts hold (directly or as the underlying of an option)
  or have open orders in, 50 per request, through the same rate limiter as everything else.
- **When.** Fetched on the poller thread, never on the tick: once per New York day per symbol
  (and at once for a symbol that starts being held), over the earliest date the lifecycle asked
  for (at least 10 days back) to 120 days ahead. Because the documentation describes `start`/`end`
  by process date (a dividend's process date is its pay date), the query runs from a week before to
  120 days after, and records are matched on ex-date.
- **Waiting, not skipping.** At each New York midnight the lifecycle asks for the new day. If that
  day's data has not been fetched on that date yet (for example the request is in flight or Alpaca
  answers 5xx or 429), the day roll stops before the day: no interest accrual, settlement, dividend
  or split for it is done, so nothing happens twice, and it is retried on the next tick. Failed
  requests are retried after 30 s, 60 s, 2, 5 and then every 15 minutes. `/api/status` shows
  `day_roll_waiting` and `corporate_actions` (state, message); the event stream says why it waits.
  An error that retrying will not fix (HTTP 400, 401, 403, 404, 422, for example a plan without
  corporate actions) is reported once ("Corporate actions are not available …") and the days roll
  on without them; it is retried hourly.
- **What is applied.** Cash dividends (ex-date, pay date; a missing or earlier pay date becomes the
  ex-date; a special dividend counts separately from a regular one) and capital gains distributions
  (long + short term rate, paid as a cash dividend). Forward and reverse splits with their ratio
  (decimal rates become an exact ratio, 1.5 for 1 is 3-for-2). Dividends in a currency other than
  USD, and every other kind (stock dividends, spin-offs, mergers, name changes, redemptions, unit
  splits, worthless removals, rights, partial calls, reorganisations) are **not** applied: they are
  shown as a "corporate action" notice on the affected position (`notices` in the position row,
  with the reason) and in the account log. The position is left as it is.
- **Late records.** Alpaca warns that records can appear after the fact. A dividend or split found
  up to 10 days after its ex-date (never before the day the accounts started) is applied on the
  next day roll and logged as late; holders are the positions at that time (an approximation).
- **Exactly once.** Every action has the id `alpaca:<Alpaca id>` and a revision key (symbol, kind,
  ex-date, regular or special). Both are saved with the accounts (`state.json`). A record seen
  again, re-issued under a new id, or amended after it was applied (different rate or ratio) is
  never applied a second time; an amendment is logged ("The data provider changed the … The
  original terms were kept"). Whether Alpaca keeps the id when it amends a record is **UNVERIFIED**;
  the revision key covers both cases.
- **Prices after a split.** Bars are requested split-adjusted, so the cached bars are dropped and
  fetched again. The current quote, the stored state and recent prints are scaled by old/new at the
  split so nothing shows the old price until the next update. The previous close: whether Alpaca's
  snapshot adjusts the previous daily bar on the ex-date is **UNVERIFIED**, so when a split took
  effect after that bar, PaperDesk keeps whichever of the raw or the adjusted value is closer to
  today's trades or quote (a split ratio is far from 1, so they are easy to tell apart). Option
  listings and option quotes on the old strikes are dropped and reloaded. The feed remembers the
  splits it has reflected in `market.json`, so a restart neither repeats nor forgets them.

### Margin checks around corporate actions

A real split changes the price before or without the positions changing (if the day roll waits, or
Alpaca publishes the split late). So, per account:
- While the start of a day waits for corporate action data, or while a held symbol has a split
  Alpaca has published (ex-date in the last 10 days, after the accounts started) that is not applied
  to the accounts yet, no margin call is started, cleared or enforced. The summary's
  `margin_paused` says why, and an event is sent once.
- For 5 days after an action PaperDesk cannot apply (merger, spin-off, ...) on a held symbol,
  margin calls are still shown but liquidation does not run ("Margin call liquidation paused: …"),
  because the price change may not be a real loss.

### Closing price for a date

`feed.closing_price(symbol, day)` returns `ClosePrice(ready, price, note)` (in `marketdata/feed.py`).
On Alpaca it is the unadjusted daily bar close (SIP, IEX if refused), fetched by the poller thread
through the shared rate limiter once the day's close is 16 minutes past, and cached for good. It
never blocks: until the answer is in it returns `ready=False`; `ready=True, price=None` means there is
no bar for that day. The simulated feeds answer from their stored daily bars. Meant for settling
option expiry against the official close.

## Practising on real past days

`python -m app.marketdata.replay fetch --date 2026-03-13 --symbols SPY,AAPL --out real-day` (from
`backend/`, with the Alpaca keys in `.env`) builds a replay session from Alpaca's historical 1-minute
bars for that day's regular session, and saves `real-day.pdsession.gz`. Put it in
`<PAPERDESK_DATA>/sessions/` to see it in the Replay list.

- Bars come from the consolidated feed (`--feed sip`, the default; free for past days because the
  end is more than 15 minutes old). If SIP is refused, IEX bars are used and the session says so.
- Prices are as traded that day (`adjustment=raw`). The 260 earlier daily bars for the chart are
  fetched split-adjusted and rescaled to that day's basis, so a later split shows no jump.
- The session header says exactly what it is: `simulated: false`, `source` "Alpaca historical
  1-minute bars, SIP feed; prices as traded on …", `built_from: "1-minute bars"`, and a
  description of the approximation (four prints per bar and quotes one spread wide, from
  docs/REPLAY.md): it is not tick data, so fills inside a minute are approximate.

## First run with real keys: checklist

Nothing here has run against Alpaca with a real key. Please do this once and send back the output
if anything says FAIL or looks wrong.

1. Put `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` in `.env` (see "Sign-up and setup" above).
2. From `backend/` run `.venv/bin/python -m app.marketdata.alpaca --check` (on Windows
   `.venv\Scripts\python -m app.marketdata.alpaca --check`). Best during market hours, and once
   outside them. Optional: `--thin=SYMBOL` picks the thinly traded symbol (default IEP).
3. Expect one line each: `assets`, `quote`, `bars`, `option chain`, `option contracts`,
   `corporate actions`, `thin symbol snapshot`, `replay session from bars`, `stream`. The output
   never contains your keys (they are also masked in error text), so the whole output can be sent.
4. Start PaperDesk with `./run.sh`. Check the banner says "real-time IEX prices via Alpaca", buy a
   few shares of SPY, open the SPY option chain, and look at `/api/status` (the `stream`,
   `corporate_actions` and `day_roll_waiting` fields).
5. After the first New York midnight with positions open, check `/api/status` again:
   `day_roll_waiting` should be `null` within a minute or so, and `corporate_actions.state` `ok`.
6. Build one replay day: `.venv/bin/python -m app.marketdata.replay fetch --date <a recent trading
   day> --symbols SPY --out check-day`, then `.venv/bin/python -m app.marketdata.replay inspect
   check-day.pdsession.gz`.

If something fails, send: the complete `--check` output; the `/api/status` JSON; the last lines
the server printed (they never include keys); for the replay step, the command and its output; and
whether the market was open at the time.

## Research data shapes

`GET /api/research/news/{symbol}`, `/calendar/{symbol}`, `/corporate-actions/{symbol}?start=&end=&types=`,
`/fundamentals/{symbol}` and `/sources`. Each block is an envelope:

```json
{"source": "Alpaca", "as_of": 1795500000.0, "available": true, "stale": false, "reason": null, "items": []}
```

`available: false` comes with a `reason` (for example, no key or the synthetic feed). `stale: true`
means a refresh failed and saved data is shown. Fundamentals use `data` in place of `items`.

**Corporate actions (for the dividend and split processing stream).** In process, call
`ex.feed.research.corporate_actions(symbol, start: date, end: date, types=None)["items"]`. The
HTTP route returns the same records with numbers as JSON numbers. Each record:

| Field | Type | Notes |
| --- | --- | --- |
| `type` | str | `cash_dividend`, `stock_dividend`, `forward_split`, `reverse_split`, `unit_split`, `spin_off`, `cash_merger`, `stock_merger`, `stock_and_cash_merger`, `redemption`, `name_change`, `worthless_removal`, `rights_distribution`, `partial_call`, `reorganization`, `capital_gains_distribution` |
| `id` | str | Alpaca's id |
| `symbol` | str | affected listing (for mergers the acquiree, for spin-offs the source) |
| `ex_date`, `record_date`, `payable_date`, `process_date`, `effective_date` | `YYYY-MM-DD` or None | |
| `rate` | Decimal | cash per share for `cash_dividend`; shares per share for `stock_dividend` |
| `old_rate`, `new_rate` | Decimal | splits: `new_rate` shares for every `old_rate` (10-for-1 is `new_rate=10, old_rate=1`) |
| `special`, `foreign` | bool | cash dividends |
| `currency` | str | empty can mean USD |
| `source` | `"alpaca"` | |
| (others) | | every other Alpaca field is passed through unchanged; `*_rate` and `price` fields are Decimal |

Earnings records: `{type: "earnings", symbol, date, hour ("before open" | "after close" | "during
market hours" | ""), quarter, year, eps_estimate, eps_actual, revenue_estimate, revenue_actual,
source: "finnhub"}` (Decimal or None). News records: `{id, headline, summary, source, url,
author, created_at, symbols, image, provider}`.

## Tests and fixtures

`backend/tests/test_provider_actions.py` covers corporate actions through the adapter end to end
(dividend, split and unsupported notice; provider late or failing; plan without the endpoint;
restart, amended and re-issued records), real option listings (validation, chain, expirations,
multi-leg, Wednesday expiry and exercise, delisted held contracts), missing prices, symbol list
bounds, replay sessions from provider bars and the check script.
`backend/tests/test_alpaca.py` and `backend/tests/test_data_sources.py` cover parsing to
Decimal, stream handling (auth failure, symbol limit, connection limit, disconnect and
resubscribe), the rate limiter and 429 backoff, fallback to synthetic, research routes, and
full order round trips against adapter quotes. Saved responses are in
`backend/tests/fixtures/`. `SOURCES.txt` there names the documentation page each one came from.
Two fixtures, the asset list and `corporate_actions.json`, are constructed from the documented
schema; `corporate_actions_examples.json` holds the per-kind examples the corporate actions page now
documents, verbatim. Two Finnhub samples had a trailing comma that made the
published sample invalid JSON; it was removed.

## What has not been tested against the live service

- Any authenticated call: no key was available. Response shapes come from documentation
  examples only.
- Real streaming data (quotes and trades arriving), the 30-symbol limit in practice (whether the
  trades and quotes channels count once or twice per symbol), and the reconnect path after a real
  drop. The handshake and the 402 error were seen live with a dummy key.
- `paper-api.alpaca.markets` (asset list, option contracts): not reachable from the build
  environment.
- Whether the free plan includes corporate actions, news, `delayed_sip`, and free SIP history
  older than 15 minutes for daily bars. The adapter falls back to IEX bars if SIP is refused.
- Units of Alpaca's greeks (assumed per day for theta and per volatility point for vega, matching
  the model).
- Corporate actions with a real key: whether the free plan includes them; whether `start`/`end`
  filter on process date or ex-date (the query is widened to cover either); whether an amended record
  keeps its id; when records for a given ex-date first appear; the name-change and merger record
  `symbols` matching (which symbol a query must name to see them).
- The quote age thresholds against real IEX update rates: how often liquid symbols go 120 seconds
  without a new quote or trade in the regular session (each such moment refuses market orders).
- Daily bars requested for a single date (`start=day`, `end=day+1`) for `closing_price`: that the
  bar's time stamp is that New York date was taken from the documentation example only.
- Whether the stock snapshot's previous daily bar is split-adjusted on an ex-date (handled either
  way by the closest-price rule, but not observed).
- Option contracts on `paper-api.alpaca.markets`: real listing size per underlying (SPY has many
  thousand contracts; PaperDesk reads up to 5 pages of 10000), the `root_symbol`/`size` of adjusted
  contracts in practice, and whether expired contracts vanish from `status=active` on the expiry
  date or the day after.
- How often IEX books are one-sided in practice, and so how often market orders are refused or wait
  under the 60-second rule; pre-market snapshots of thin symbols.
- Historical 1-minute SIP bars for a past day on the free plan (assumed free because the end is
  older than 15 minutes), and their volume compared with IEX.
- Finnhub: every call, and the free per-minute limit.
