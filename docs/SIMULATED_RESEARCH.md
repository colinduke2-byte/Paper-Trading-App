# Simulated research

With no data provider key, PaperDesk runs on 26 fictional companies (`backend/app/marketdata/synthetic.py`).
The Research and Company tabs are filled from `backend/app/marketdata/simresearch/`, so simulated mode is a
complete practice environment. Everything is labelled **Simulated**: the source line of every block says
"Source: Simulated (fictional companies and events)", and every record carries `simulated: true`. No
real company, person, organisation or economy is described or named.

## Determinism

Everything scheduled is a pure function of the feed seed (`PAPERDESK_SEED`) and the time asked about:
statements, estimates, reported results, analyst rating changes, economic releases, and the price move
each release causes. The same seed and time always give the same content, across restarts and in a
replay (under the simulated clock, including seeks). News about price action (large daily moves, first
new 52-week high or low, sector moves) is read from the stored daily bars, which are saved with the
market state.

A bundled replay session from the synthetic feed gets the research of its seed (read from the session's
`source`, for example "synthetic feed, seed 11"). Sessions of other symbols have none, as before.

## What is generated

| Item | Rules |
| --- | --- |
| Statements | Income statement, balance sheet and cash flow per calendar quarter, plus fiscal years. Revenue grows at a seeded rate with sector seasonality; profit has seeded noise. The four quarters ending Q2 2026 add up to the listed EPS times the listed shares. Lines are whole-dollar Decimals and totals are sums of the rounded lines, so assets = liabilities + equity and operating + investing + financing = change in cash exactly. Per-share figures use the current share count (adjusted for splits). |
| Ratios | P/E, P/S, P/B, margins, return on equity, debt/equity, payout ratio, revenue and earnings growth, all from the trailing four reported quarters and the current price. |
| Funds | Holdings by market value (the named sector, plus Communication for the technology fund, or every stock), sector weights, P/E of holdings, expense ratio. No statements or earnings. |
| Earnings | One report per quarter, 18 to 40 days after quarter end (fourth quarters later), before the open (8:00) or after the close (16:05) New York time. Estimates are published ahead; the result (beat, miss or in line) comes from the seed. |
| Economic calendar | Jobs report (first Friday), consumer prices (second Tuesday), retail sales (16th), growth (last Thursday of Jan, Apr, Jul, Oct), all 8:30; central bank rate decision (last Wednesday of eight months), 14:00. Each series follows a seeded path from 2016. |
| News | Earnings previews and results, dividend declarations, split announcements, analyst rating changes, economic releases, large moves, 52-week highs and lows, sector moves. Each item names the event it refers to (`event`), and only events at or before the current time appear. |

## Effects on prices and options (synthetic feed only)

- An earnings report gaps the price by a seeded log return (standard deviation about 2.2 to 3.4 times
  the stock's daily volatility, driven by the surprise and the outlook). An economic release moves every
  symbol by a seeded market move times its beta.
- The feed applies each release once, at its time, and saves how far it has processed, so a release that
  fell while PaperDesk was stopped is applied on the first tick after the restart. Releases from before
  the feed first started are history and move nothing.
- The gap reaches orders like any quote: a resting stop beyond the gap triggers and fills at the gapped
  market, not at the stop price. In `us` session mode an after-close report gaps the post-market quote
  and stops fill at the next regular open.
- Option volatility gets an event term: for expiries after an upcoming report, the gap variance is added
  to the surface (`options/chain.py`, `surface_vol(..., event_var)`), so implied volatility rises into
  the report and drops once it has passed. The feed supplies it through
  `MarketDataProvider.event_variance(symbol, expiry_ts, now)` (zero by default).
- A replay plays recorded prices, so releases there move nothing; the calendar and the option event term
  still follow the session's seed.

## API

Existing routes (`/api/research/news/{symbol}`, `/calendar/{symbol}`, `/corporate-actions/{symbol}`,
`/fundamentals/{symbol}`, `/sources`) return the same envelopes as with a live provider. New routes:

- `GET /api/research/news?limit=&category=`: market-wide news.
- `GET /api/research/statements/{symbol}`: `data.quarterly` and `data.annual`, each with `income`, `balance`, `cash_flow`.
- `GET /api/research/economic-calendar?start=&end=` (default 14 days back, 45 ahead): `actual` stays null until the release time.
- `GET /api/research/earnings-calendar?start=&end=` (default 14 days back, 75 ahead): every report in the window.

A live provider without these returns `available: false` with the reason.

## Known limits

- The screener and the quote header still use the listed EPS and share count from the symbol list; the
  Company and Research tabs use the trailing figures from the statements. They differ slightly.
- Upcoming-earnings alerts are a notice on the earnings calendar for held symbols (reports within two
  weeks), not entries in the alert engine.
