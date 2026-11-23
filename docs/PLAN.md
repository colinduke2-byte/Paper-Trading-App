# Paper trading platform: build plan

Built on the Multi-agent project playbook v4. This document is the draft of `PLAN.md` (playbook Phase 0, step 1) plus every value needed to fill in the lead prompt. Working name: **PaperDesk** (change freely).

Status: draft for owner decisions. Nothing has been built and no repository exists yet.

---

## 1. Owner decisions needed before kickoff

These are the Phase 0 step 4 question batch. Each has a recommended default; the rest of the plan assumes the defaults.

| # | Decision | Recommended default | Alternatives | What it changes |
|---|---|---|---|---|
| D1 | Form factor | Web app you run yourself (browser UI, local server) | Desktop app (Tauri wrapper, adds one stream); hosted multi-user site (adds auth, data licensing, hosting) | Streams S6, S12 |
| D2 | Asset classes in v1 | US stocks, ETFs and listed equity options | Add crypto (one adapter, 24/7 session rules); futures and forex are next-version | Streams S1, S4, S5 |
| D3 | Market data | One live provider behind an adapter, free tier first (Alpaca is the working assumption: real-time IEX stock quotes and an indicative options feed on its free plan) | Paid consolidated feed (true NBBO, real Level 2, OPRA options); delayed-only data | Realism of quotes and depth; needs an API key from you |
| D4 | Users | Single user, several paper accounts | Multi-user with logins | Auth scope in S6; data licence terms |
| D5 | Deploy target | Docker Compose on your own machine, tagged GitHub release | A small cloud VM | Stream S12, release gate (c) |
| D6 | Final publish | "Ping me and wait for my go" | Pre-authorised once gates are green | Lead prompt |
| D7 | Fidelity rule | Realism over convenience: when the simulator must choose, it fills conservatively (worse price, later fill) | Optimistic fills | Fill model defaults in S2 |
| D8 | Historical replay mode in v1 | Yes: practise on any past session at 1x to 60x speed | Defer | Small, because the replay feed is needed for testing anyway |

Provider plans and limits change; the lead verifies D3 against the provider's current terms at kickoff rather than trusting this table.

Things you must supply: a GitHub repository (empty is fine), the data provider API key (stored as an environment secret, never in the repo), and the provider's host added to the cloud environment's network allowlist.

---

## 2. Product scope: what "everything a traditional platform gives you" means

### 2.1 Market information

| Area | v1 contents |
|---|---|
| Level 1 quote | Bid, ask, sizes, last, change, volume, open/high/low/previous close, VWAP, 52-week range, average volume, market status |
| Depth and tape | Level 2 order book and time and sales. Real depth where the provider supplies it; otherwise synthesised depth, always labelled "simulated depth" |
| Charts | Candles, bars, line, area; 1 min to monthly; extended-hours shading; volume; indicators (SMA, EMA, VWAP, Bollinger, RSI, MACD, ATR, stochastic); drawing tools (trend line, horizontal, Fibonacci); orders and positions drawn on the chart; compare symbols |
| Options | Full chain by expiry: bid/ask/last, volume, open interest, implied volatility, delta, gamma, theta, vega, rho; IV rank; expected move; probability in the money |
| Fundamentals | Company profile, market cap, P/E, EPS, dividend and yield, beta, float, short interest where available, income statement, balance sheet, cash flow, filings links |
| News and calendars | Per-symbol and market news, earnings calendar, dividend and split calendar, economic calendar |
| Discovery | Watchlists (several, with custom columns), screener (price, volume, fundamentals, technical filters), top movers, sector heatmap, symbol search |
| Alerts | Price, percent change, volume, indicator cross, order fill, margin warning |

### 2.2 Trading

| Area | v1 contents |
|---|---|
| Order types | Market, limit, stop, stop-limit, trailing stop (amount and percent), market-on-open, market-on-close, limit-on-open, limit-on-close |
| Time in force | Day, GTC, GTD, IOC, FOK, extended-hours flag |
| Linked orders | Bracket (entry plus target plus stop), OCO, OTO |
| Sides | Buy, sell, sell short, buy to cover; options buy/sell to open/close |
| Options strategies | Single leg, verticals, calendars, diagonals, straddles, strangles, iron condors, butterflies, covered calls, cash-secured puts, custom up to four legs with a net debit or credit price |
| Order handling | Preview with estimated cost, fees, buying-power effect and margin impact; modify; cancel; cancel-all; flatten position; fractional shares for stocks |
| Fill simulation | Fills against the live quote, size-aware partial fills, configurable slippage and latency, queue-position model for resting limits, no fills outside the session unless flagged, rejects with real-world reasons |
| Costs | Configurable commission schedule, per-contract option fees, regulatory fees on sales, margin interest, short borrow fees |

### 2.3 Account

| Area | v1 contents |
|---|---|
| Account types | Cash and margin; several paper accounts; set starting balance; reset |
| Balances | Cash, settled cash, equity, stock and option buying power, day-trading buying power, maintenance requirement, excess liquidity |
| Rules | Reg T initial and maintenance margin, short-sale margin, option margin by strategy, options approval levels, pattern day trader flag, T+1 settlement, margin calls and forced liquidation. Each rule can be turned off per account |
| Positions | Quantity, average cost, mark, market value, day and total profit and loss, percent of account, per-position greeks, tax lots |
| Lifecycle | Splits, cash dividends, optional dividend reinvestment, option expiry, automatic exercise, simulated early assignment, symbol changes |
| History and reports | Order and fill history, activity ledger, realised gains by lot (FIFO, LIFO, specific lot), wash-sale flag, monthly statements, CSV export |
| Performance | Equity curve, time-weighted return, benchmark comparison, drawdown, Sharpe, win rate, average win and loss, profit factor, trade journal with notes and tags |

### 2.4 Paper-only features

- Historical replay: trade any past session with the clock sped up, paused or stepped.
- A permanent "Paper trading: simulated, no real money" banner, and a data-source and delay label on every quote.

### 2.5 Out of scope for v1 (seed of `NEXT_VERSION_LIST.md`)

Futures, forex, bonds, mutual funds, real broker connectivity, strategy backtester and scripting language, mobile app, social features, multi-user hosting, conditional orders on other symbols, portfolio margin, tender offers, mergers and spin-offs.

---

## 3. Architecture

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy, Alembic | Exact decimal arithmetic, strong numeric libraries for option pricing |
| Database | PostgreSQL (SQLite for unit tests) | Transactions for the ledger |
| Streaming | WebSocket from backend to browser; one in-process event bus | Quotes, order updates, balances |
| Frontend | React, TypeScript, Vite, TradingView Lightweight Charts (open source) | Standard trading-UI stack |
| Tests | pytest plus Hypothesis (property tests), Vitest, Playwright end to end | The playbook requires automated acceptance checks |
| Packaging | Docker Compose, GitHub Actions CI | Reproducible build for the release gate |

Design rules that every stream inherits:

1. **Money is never a float.** All prices, quantities and cash use the `Money` and `Qty` decimal types from `core/`.
2. **One clock.** All code asks `core.clock` for the time. The clock is real in production, and simulated in tests and replay. This is what makes every acceptance check deterministic.
3. **Data behind one interface.** `MarketDataProvider` has three implementations: `SimFeed` (scripted, for tests), `ReplayFeed` (recorded sessions), and the live adapter. No other module knows which is running.
4. **The ledger is the only source of truth for money.** Double entry and append only. Balances and positions are derived from it, never stored separately and edited.
5. **Event flow in one direction:** market data → matching engine → `Fill` event → ledger → portfolio and risk → API stream. Modules talk through the typed events in `core/events.py`.
6. **Contract first.** The REST and WebSocket schema (`contracts/openapi.yaml`, `contracts/events.schema.json`) is written in the first stream, so frontend streams build against a mock server while the backend is still in progress.

---

## 4. Playbook section 0: tool and model map

Checked against the session this plan was written in; the lead rechecks at Phase 0, step 0.

| Name in prompts | Value here | Note |
|---|---|---|
| `[START_SESSION]` | `create_session` | Present |
| `[MESSAGE]` | `send_message` | Present |
| `[READ_INBOX]` | `ReadNotifications` | Present |
| `[SESSION_STATUS]` | `get_session` / `list_sessions` / `list_events` | Present |
| `[SCHEDULE]` | `create_trigger` / `update_trigger` | Present |
| `[NOTIFY_OWNER]` | `PushNotification` | Present |
| `[PR]` | `gh pr create` / `gh pr merge` / `gh pr edit --add-label` | Needs the repository attached with push access |
| `[USAGE]` | none | Use the cap-count budget rule |
| `[MODEL_LEAD]` | `claude-opus-5-5` | |
| `[MODEL_WORKER]` | `claude-sonnet-5-5` | |
| `[MODEL_REVIEW]` | `claude-fable-5-2` | Fallback `claude-opus-5-5` |
| `[MODEL_CHEAP]` | `claude-haiku-5-5` | |

**GPU routing:** this project has no GPU work. Module A is skipped. From Module B only the look checks (screenshot comparisons, done in cloud Playwright) and a non-GPU performance gate are used.

---

## 5. Streams

One owner per directory. No two streams edit the same files. "HARD" follows the playbook test: a subtle algorithm or state problem where Fable's judgement decides the outcome. Each HARD core is split out to be as small as possible.

| ID | Stream | Owns | Depends on | HARD core |
|---|---|---|---|---|
| S0 | Foundation and contracts | `backend/app/core/`, `contracts/`, `backend/alembic/`, repo scaffold, CI, `CLAUDE.md` | none | no |
| S1 | Market data | `backend/app/marketdata/` | S0 | no |
| S2 | Orders and matching engine | `backend/app/orders/`, `backend/app/matching/` | S0, S1 | **yes**: `matching/engine.py` (order state machine, trigger logic, partial fills, linked orders) |
| S3 | Ledger, portfolio, margin | `backend/app/ledger/`, `backend/app/portfolio/`, `backend/app/risk/` | S0 | **yes**: `risk/margin.py` (buying power and requirement calculation across stock, short and option positions) |
| S4 | Options | `backend/app/options/` | S0, S1, S2, S3 | **yes**: `options/lifecycle.py` (expiry, exercise, assignment against multi-leg positions) |
| S5 | Lifecycle and corporate actions | `backend/app/lifecycle/` | S3 | no |
| S6 | API and streaming | `backend/app/api/`, `backend/app/auth/` | S0, then S1 to S5 as they land | no |
| S7 | Frontend shell and trading UI | `frontend/src/app/`, `frontend/src/features/trade/`, `frontend/src/features/account/` | S0 contract | no |
| S8 | Charts and market views | `frontend/src/features/charts/`, `quote/`, `depth/`, `watchlist/` | S0 contract | no |
| S9 | Options UI | `frontend/src/features/options/` | S0 contract, S7 | no |
| S10 | Research data | `backend/app/research/`, `frontend/src/features/research/` | S1 | no |
| S11 | Analytics, reports, alerts | `backend/app/analytics/`, `backend/app/alerts/`, `frontend/src/features/reports/` | S3 | no |
| S12 | Packaging and release | `docker/`, `scripts/`, `docs/`, `e2e/` | all | no |

### 5.1 Stream detail and acceptance checks

The lead writes each check before briefing the worker, commits it on the stream branch as the BASE SHA, and the worker may not edit it. All checks live under `tests/acceptance/<stream>/`.

**S0 Foundation and contracts.** Repo layout, `Money`/`Qty` types, simulated clock, US market calendar (sessions, half days, holidays), event types, database migrations, API contract, mock API server generated from the contract, CI.
- Check: `test_core.py`. Decimal types reject floats; the calendar returns correct open and close for 20 listed dates including half days and holidays; the mock server serves every path in `openapi.yaml`; CI runs lint, type check and tests green.

**S1 Market data.** Provider interface, `SimFeed`, `ReplayFeed`, live adapter, symbol master, bar aggregation from ticks, quote cache, rate limiting, reconnect, staleness flag, session recorder for building replay files.
- Check: `test_marketdata.py`. A recorded tick file aggregates into 1 min, 5 min and daily bars that match a golden file exactly; a dropped connection reconnects and resubscribes; quotes older than the threshold are flagged stale; the live adapter passes a contract test against saved provider responses (no network in tests).

**S2 Orders and matching engine.** Order validation, order state machine, all order types and time-in-force values from 2.2, linked orders, fill model (slippage, latency, partial fills, queue position), rejects, modify and cancel.
- Check: `test_matching_scenarios.py` runs about 60 scripted scenarios (`scenarios/*.yaml`: a quote tape plus orders, with the exact expected fills, times and states). `test_matching_properties.py` holds Hypothesis invariants: a limit order never fills worse than its limit; filled quantity never exceeds order quantity or available size; no fill happens before the order's arrival time plus latency (no look-ahead); a stop never triggers without a qualifying print; exactly one leg of an OCO can fill; a FOK fills fully or not at all; every order ends in exactly one terminal state.
- HARD core: Fable BUILD writes `engine.py` against the scenarios. A worker then does validation, persistence, wiring and extra tests.

**S3 Ledger, portfolio, margin.** Double-entry ledger, positions and tax lots derived from it, settlement, buying power, Reg T and maintenance requirements, short margin, pattern day trader tracking, margin call and liquidation, fees and interest postings.
- Check: `test_ledger_invariants.py`: every posting set sums to zero; replaying the ledger from empty reproduces the current balances exactly; equity equals cash plus marked positions to the cent after any random sequence of fills. `test_margin_reference.py`: about 40 worked examples with hand-calculated answers (long stock on margin, short stock, concentrated position, margin call threshold, day-trading buying power, cash-account settlement violation).
- HARD core: Fable BUILD writes `margin.py`. Its option-strategy cases are finished in S4.

**S4 Options.** Contract symbology (OCC format), chain assembly, Black-Scholes and binomial (American) pricing, greeks, implied volatility solver, multi-leg orders with net pricing, strategy margin, expiry processing, exercise and assignment.
- Check: `test_pricing_reference.py`: prices and greeks match published textbook reference values to 1e-4; put-call parity holds for European cases; the IV solver round-trips price → IV → price across a grid including deep in and out of the money. `test_option_lifecycle.py`: scripted expiries for each strategy in 2.2 produce the exact expected positions and cash (for example, a short vertical with the stock pinned between strikes leaves the right stock position and margin).
- HARD core: Fable BUILD writes `lifecycle.py`.

**S5 Lifecycle and corporate actions.** End-of-day job, splits and reverse splits (positions, lots, open orders, option adjustments), cash dividends on ex-date and pay-date, dividend reinvestment, margin interest and borrow fee accrual, GTC and GTD expiry, symbol changes.
- Check: `test_corporate_actions.py`: a 4-for-1 split, a 1-for-10 reverse split with a fractional remainder, and a dividend on a long and a short position each produce golden ledger output; total account value is unchanged across a split.

**S6 API and streaming.** REST endpoints and WebSocket channels implementing the S0 contract, single-user auth token, account management, idempotent order submission, error model.
- Check: `test_contract.py`: every response validates against `openapi.yaml`; a duplicate order submission with the same client id creates one order; a WebSocket client receives quote, order and balance events in order with no gaps after a forced reconnect.

**S7 Frontend shell and trading UI.** Layout with dockable panels, symbol search, order ticket with preview, positions, open orders, order history, balances, account switcher, hotkeys, paper banner.
- Check: Playwright `trade.spec.ts` against the mock server: place, modify and cancel a limit order; submit a bracket; flatten a position; the preview shows cost, fees and buying-power effect matching the mock's numbers; the paper banner is visible on every route.

**S8 Charts and market views.** Chart with timeframes, indicators and drawings, orders and positions on chart, quote panel, depth ladder, time and sales, watchlists.
- Check: `charts.spec.ts`: indicator values rendered for a fixed bar set equal golden values from `indicators.golden.json` (unit-tested separately to 1e-6); screenshot comparison at three fixed views with a pixel-difference threshold; a streaming tick updates the last candle; the depth panel shows the "simulated depth" label when the feed says so.

**S9 Options UI.** Chain view, expiry and strike filters, strategy builder, payoff diagram with break-evens, multi-leg ticket, roll and close actions.
- Check: `options.spec.ts`: building an iron condor from the chain produces the correct four legs and net credit; payoff diagram maximum profit, maximum loss and break-evens equal the calculated values for five strategies.

**S10 Research data.** Fundamentals, news, calendars, screener, movers, sector heatmap, with caching and source labels.
- Check: `test_research.py` against saved provider responses: the screener returns the exact expected symbol set for six filter combinations on a fixed dataset; every panel shows its source and timestamp; a provider outage shows a stale-data state rather than an error page.

**S11 Analytics, reports, alerts.** Equity curve, return and risk statistics, benchmark comparison, realised gain report by lot method, wash-sale flag, statements, CSV export, trade journal, alert engine.
- Check: `test_analytics_reference.py`: time-weighted return, drawdown and Sharpe for a fixed ledger match hand-calculated values; FIFO, LIFO and specific-lot reports match golden files; an alert fires exactly once when its condition is crossed on the simulated clock.

**S12 Packaging and release.** Docker Compose, seed data and a bundled replay session for first run, full end-to-end suite, user guide, reproducible build script, rollback script.
- Check: `e2e/full_day.spec.ts`: from a clean `docker compose up`, replay one recorded session at high speed and run the "day in the life" script (stock trades, a short, a bracket, an option spread held through expiry, a dividend); the final statement equals a golden statement to the cent. Two builds from the same commit produce identical image digests.

### 5.2 Order of work

| Wave | Streams | Gate to next wave |
|---|---|---|
| 0 | S0 alone | Contract and core types merged |
| 1 (start small) | S1, S2 (Fable BUILD of the engine first), S3 (Fable BUILD of margin first), plus QA | First review cycle runs cleanly; then add the RC integrator |
| 2 | S6, S7, S8, S5 | Stock trading works end to end on replay data: **milestone M1** |
| 3 | S4 (Fable BUILD of lifecycle first), S9, S10, S11 | Options and research complete: **milestone M2** |
| 4 | S12, then freeze | Release candidate |

Frontend streams S7 and S8 can start in wave 1 against the mock server if session caps allow.

---

## 6. Lead prompt fill-ins

| Placeholder | Value |
|---|---|
| Project name | PaperDesk |
| Goal | Ship v1: a self-hosted paper trading platform for US stocks, ETFs and options with the information and order options of a traditional broker platform |
| Repo | to be supplied by owner |
| Backup command | `git push origin --tags && git bundle create <BACKUP PATH>/paperdesk-$(date -u +%Y%m%d-%H%M).bundle --all` |
| Top priority | Correctness of money and fills over features and speed (D7) |
| Standing rules | No real orders and no broker order-entry endpoints, ever. No float arithmetic for money. Every quote shows its source and delay. Paper banner always visible. No API keys in the repo, logs or PRs. Do not change the API contract or UI layout without asking. Scope exclusions in 2.5 are locked |
| Deploy target | Tagged GitHub release plus Docker Compose bundle (D5) |
| Final publish | Ping me and wait for my go (D6) |
| Per-stream budget | 2 attempts or about 4 hours |
| Run caps (no usage tool) | 72 hours wall clock, 30 worker sessions, 12 Fable jobs of which at most 4 BUILDs. Adjust to your plan |
| BUILD time box | About 3 hours |
| CORE files (mandatory Fable review) | `backend/app/core/money.py`, `core/clock.py`, `core/events.py`, `matching/`, `ledger/`, `risk/`, `options/lifecycle.py`, `options/pricing.py`, `contracts/`, `backend/alembic/`, `.github/workflows/`, `docker/`, `scripts/release*` |
| Primary quality target (watchdog step 6) | Ledger invariants and the matching property tests are green on main at every check |
| Morning report time | to be supplied by owner |
| Rollback path | `releases/previous/` (previous image digests and Compose file) |

Planned Fable jobs: 1 plan review, 3 BUILDs (S2, S3, S4 cores), 1 RC review, leaving 7 for mandatory PR reviews of core files, diagnoses and consults. The three BUILD PRs are reviewed by a new Opus session, as the playbook requires.

---

## 7. Release gate

| Gate | Contents |
|---|---|
| (a) Cloud gate | Full backend and frontend suites; all acceptance checks; Hypothesis property tests at an increased example count; Playwright smoke of every main path (search, quote, chart, stock order, bracket, short, option spread, expiry, reports, account reset) |
| (a) Project-specific | Reconciliation run: replay three recorded sessions with a randomised order script and assert ledger invariants after every event. Determinism: the same replay and script twice gives byte-identical ledgers. Secret scan of the repo and built images. Licence check of dependencies |
| (b) Module gates | Look checks: screenshot comparisons at fixed views. Performance: order submit to acknowledged under 100 ms at the median on replay data; chart with 10,000 bars interactive in under 1 second; quote fan-out of 200 symbols without dropped frames. No GPU gate |
| (c) Pre-publish smoke | `docker compose up` from exactly the release bundle on a clean machine, first-run seed, the S12 full-day script, and a 15-minute live-feed soak during market hours with the real key |
| (d) Fable RC review | Once, after (a) to (c) are green |

Known failures at start: none.

---

## 8. Risks

| Risk | Effect | Handling |
|---|---|---|
| Free data is not the full market (single-exchange quotes, indicative option prices, no real depth) | Fills are less realistic than a paid feed | Label the source everywhere; conservative fill defaults; the adapter makes a paid feed a one-stream change later |
| Provider terms restrict display or redistribution | A hosted multi-user version could breach them | v1 is single user with the user's own key (D4) |
| Fill realism is a judgement call | Users over-trust paper results | Fill model settings are visible and documented; the guide states the limits plainly |
| Margin and option-assignment rules are intricate | Wrong balances | HARD cores with hand-worked reference cases; ledger invariants run on every check |
| Tests that depend on live data are flaky | False reds | No test touches the network; live behaviour is checked only in gate (c) |
| Cross-stream interface drift | Integration failures late | Contract and event types owned by S0 only; rolling rc-preview from wave 1 |
| Scope is large | Run exceeds caps | M1 (stocks end to end) is shippable alone; S10 and S11 are the first cuts to `NEXT_VERSION_LIST.md` |

The product is a simulator for practice and education. It gives no investment advice and the user guide says so.

---

## 9. Kickoff sequence

1. Owner answers D1 to D8, creates the repository, and adds the data provider key and host to the cloud environment.
2. Start the lead session on `claude-opus-5-5` with the tool map (section 4) and the lead prompt filled from section 6.
3. Lead runs Phase 0: tool check, commits this file as `PLAN.md` on `ops`, one Fable plan review, `CLAUDE.md` through a PR, watchdog and backstop.
4. Wave 0 (S0), then wave 1 with QA.
