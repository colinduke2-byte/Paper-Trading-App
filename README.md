# PaperDesk

A paper trading platform: practise trading stocks and options with simulated money.
This is a first working build. It runs entirely on your own machine and needs no
account, API key or internet connection.

**All market data in this build is simulated.** The companies are fictional and their
prices follow a random walk. Option prices come from a pricing model. Nothing here is
real market data or investment advice.

## Run it

Requires Python 3.11 or newer (Node is not needed).

```bash
./run.sh
```

On Windows, double-click `run.bat` (it tries `py -3`, then `python`, and opens your browser once the
server answers; the Windows path is checked by CI but has not been tried by hand). Then open
http://127.0.0.1:8000. The first start creates a virtual environment and installs from
`backend/pyproject.toml`; later starts reuse it until `pyproject.toml` changes.

The first start creates two accounts (a 100,000 margin account and a 25,000 cash
account). State is saved in `backend/data/` and survives restarts.

> **There is no login.** Anyone who can reach the port can trade and delete your paper accounts.
> Keep PaperDesk on `127.0.0.1` and never expose it to a network or the internet.

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `PAPERDESK_SESSIONS` | `always` | `always`: the simulated market never closes. `us`: follow US market hours and holidays |
| `PAPERDESK_DATA` | `backend/data` | Where state is saved |
| `PAPERDESK_SEED` | `7` | Seed for the simulated price history |
| `PORT` | `8000` | Port (`run.sh`, `run.bat`, Docker) |
| `HOST` | `127.0.0.1` | Bind address (`run.sh`, `run.bat`). Only the Docker image sets `0.0.0.0`, inside the container |

`.env.example` lists every setting, including reserved names for the market data work.

### Docker

```bash
cp .env.example .env     # optional
docker compose up --build
```

Then open http://127.0.0.1:8000. State lives in the named volume `paperdesk-data`
(`docker compose down -v` deletes it). The compose file publishes the port on `127.0.0.1` only, which is
what keeps the unauthenticated app private; do not change that. The image was written without a running
Docker daemon available, so treat the first `docker compose up --build` as its first real test.

## What works

- **Market views:** watchlist with search, level 1 quotes, candlestick charts (1 minute
  to weekly) with SMA, EMA, Bollinger bands, VWAP and RSI, simulated depth ladder,
  time and sales, company facts, screener.
- **Orders:** market, limit, stop, stop-limit and trailing stop (amount or percent);
  day, good-till-canceled, immediate-or-cancel and fill-or-kill; bracket orders with
  take profit and stop loss; order preview, change and cancel; fractional shares.
- **Fill simulation:** fills against the live quote, partial fills limited by the
  displayed size, market orders that walk a synthetic book, stops triggered by trades.
- **Accounts:** cash and margin accounts, several at once, reset; buying power, Reg T
  style initial and maintenance margin, short selling, margin-call warning.
- **Options:** chains with weekly and monthly expiries, implied volatility and greeks,
  single-leg buying and writing, covered calls, cash-secured puts, uncovered-option
  margin, automatic exercise and assignment at expiry.
- **Records:** positions with FIFO lots, order and fill history, a double-entry ledger,
  realised and unrealised profit and loss, performance statistics, price alerts.

## What is not built yet

See `docs/PLAN.md` for the full plan. Still to do:

- Real market data (the provider interface is in `backend/app/marketdata/feed.py`;
  a live adapter needs an API key).
- Multi-leg option orders, early assignment, American-style pricing.
- Dividends, splits, margin interest, borrow fees, settlement dates, the pattern day
  trader rule, forced liquidation on a margin call.
- Market-on-open/close orders, good-till-date, standalone one-cancels-other orders.
- News, calendars, real fundamentals, statements, tax-lot method choice, CSV export.
- Historical replay mode, chart drawing tools, logins.

## Develop

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

```bash
cd frontend
npm install
npm run dev
```

`npm run dev` serves the UI on port 5173 and proxies to the backend on port 8000.
`npm run build` writes `frontend/dist`, which the backend serves.

## Tests and CI

```bash
cd backend && python -m pytest                 # backend suite
cd frontend && npm run build                   # type-check and build the UI
cd e2e && npm install && npm test              # browser tests: builds the UI, starts its own server
```

The end-to-end tests (Playwright, `e2e/`) start the server themselves on a random private port with a
temporary data directory and a synthetic feed, and fail on any console or page error. They run on a desktop
profile and on iPhone 13 and Pixel 7 emulation (`e2e/tests/mobile.spec.ts`); mobile tests that today's layout
fails are marked `test.fixme` with the reason. Environment: `E2E_PYTHON` (Python with the backend installed;
defaults to `backend/.venv`, else `python3`), `E2E_PORT`, `E2E_CHROMIUM` (path to a Chromium to use instead of
Playwright's own), `E2E_SKIP_BUILD=1`. Run `npx playwright install chromium` once if no browser is installed.

GitHub Actions (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main`: backend tests on
Python 3.11 and 3.13 (Linux), plus Windows and macOS; the UI build; the end-to-end tests; and a Windows job that
starts `run.bat` and checks that the API and UI answer.

## Layout

| Path | Contents |
|---|---|
| `backend/app/core` | Decimal money helpers, clock, US market calendar, shared records |
| `backend/app/marketdata` | Provider interface, bar storage, scripted test feed, synthetic feed |
| `backend/app/matching` | Order matching engine (pure, deterministic) |
| `backend/app/ledger` | Double-entry ledger and account with FIFO lots |
| `backend/app/risk` | Margin and buying-power rules |
| `backend/app/options` | Option symbols, Black-Scholes pricing, chain, expiry |
| `backend/app/exchange.py` | The simulated broker that wires everything together |
| `backend/app/api` | REST and WebSocket API |
| `frontend/src` | React user interface |
| `e2e` | Playwright browser tests |
| `tools` | Helper for `run.bat` |
