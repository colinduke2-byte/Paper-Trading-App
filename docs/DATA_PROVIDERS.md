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
| Listed contracts and open interest | `GET /v2/options/contracts` on the trading host (read-only) | [get-options-contracts](https://docs.alpaca.markets/us/reference/get-options-contracts.md) |
| News | `GET /v1beta1/news?symbols=` | [news-3](https://docs.alpaca.markets/us/reference/news-3.md) |
| Corporate actions | `GET /v1/corporate-actions`: dividends, splits, spin-offs, mergers, name changes and more. Whether Basic restricts it is not stated (**UNVERIFIED**, assumed free). | [corporateactions-1](https://docs.alpaca.markets/us/reference/corporateactions-1.md) |
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
   for the symbol list, a quote, daily bars, an option chain and the stream. It never prints keys.

If you reset the paper account in Alpaca's dashboard, generate new keys and update `.env`.
Only one program can use the stream for each key. If another tool is connected, PaperDesk
shows "connection limit exceeded" and falls back to polling.

### Finnhub (optional: earnings dates and fundamentals)

1. Go to <https://finnhub.io/register>, create a free account and confirm your email.
2. Copy the API key from the dashboard.
3. Add `FINNHUB_API_KEY=…` to the same `.env` file and restart.

### Where keys live

- `.env` in the project root is read first at start-up by PaperDesk itself (`app/core/envfile.py`), for every setting, so
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
| `PAPERDESK_ALPACA_CRYPTO_LOC` | `us` | Crypto data location: `us` (Alpaca US), `us-1` (Kraken US) or `eu-1` (Kraken EU). |
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
- **Quotes with a missing side.** IEX often has no bid or no ask outside regular hours. A missing
  side is filled from the last trade, so a price is never zero. A crossed quote is clamped to a
  zero spread. During regular hours this can let a market order on a thinly traded symbol fill at
  the last trade price.
- **Rate limiting.** A token bucket shared by all threads (190 per minute by default). On 429
  it blocks until `X-RateLimit-Reset`. A web request waits at most 5 to 10 seconds, then shows
  cached data or an error. 5xx responses and network errors are retried twice with backoff.
- **Charts.** Every UI timeframe maps to Alpaca bars: 1m→`1Min`, 5m→`5Min`, 15m→`15Min`,
  1h→`1Hour`, 1d→`1Day`, 1w→`1Week`. Bars are fetched newest first with `sort=desc` and cached for
  30 seconds (1m) up to 1 hour (1w). Intraday bars use IEX. Daily and weekly bars use
  split-adjusted consolidated (SIP) history ending 16 minutes ago, which is free and has full
  volume. If that is refused, they use IEX.
- **Options.** Expiries come from `/v2/options/contracts`, which also gives open interest. The
  chain comes from the option-chain snapshot within ±30% of spot. A contract the provider does
  not quote is priced by the model and marked `source: "model"`. The chain shows "Alpaca
  indicative option feed …" in place of "model prices". Option orders fill against the provider
  quote when it is less than 15 minutes old, and against the model otherwise. Contracts you hold or
  have orders on are polled every 5 seconds.
- **Symbol list.** Active, tradable US equities except OTC, cached for a day in
  `alpaca_assets.json` in the data folder. Alpaca has no ETF flag, so names containing "ETF" are
  shown as funds.
- **Never trades.** The HTTP layer implements only GET. No order endpoint exists in the code, and a
  test checks for that.

## Crypto

Alpaca's crypto market data (USD pairs, 24/7) is added to the stock feed by `AlpacaCryptoFeed`. The
verified facts, how it works and what has not been tested against the live service are in
[CRYPTO.md](CRYPTO.md#alpaca-crypto-data).

## Research data shapes

`GET /api/research/news/{symbol}`, `/calendar/{symbol}`, `/corporate-actions/{symbol}?start=&end=&types=`,
`/fundamentals/{symbol}` and `/sources`. Each block is an envelope:

```json
{"source": "Alpaca", "as_of": 1795500000.0, "available": true, "stale": false, "reason": null, "items": []}
```

`available: false` comes with a `reason` (for example, no key). On the synthetic feed the same routes serve simulated research; see `docs/SIMULATED_RESEARCH.md`. `stale: true`
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

`backend/tests/test_alpaca.py` and `backend/tests/test_data_sources.py` cover parsing to
Decimal, stream handling (auth failure, symbol limit, connection limit, disconnect and
resubscribe), the rate limiter and 429 backoff, fallback to synthetic, research routes, and
full order round trips against adapter quotes. Saved responses are in
`backend/tests/fixtures/`. `SOURCES.txt` there names the documentation page each one came from.
Two fixtures, the asset list and corporate actions, are constructed from the documented schema
because the pages have no example. Two Finnhub samples had a trailing comma that made the
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
- Finnhub: every call, and the free per-minute limit.
