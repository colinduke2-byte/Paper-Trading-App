# PaperDesk

A paper trading platform: practise trading stocks and options with simulated money.
This is a first working build. It runs entirely on your own machine and needs no
account, API key or internet connection.

**All market data in this build is simulated.** The companies are fictional and their
prices follow a random walk. Option prices come from a pricing model. Nothing here is
real market data or investment advice.

## Run it

Requires Python 3.11 or newer.

```bash
./run.sh
```

On Windows, double-click `run.bat` (not yet tested on Windows). Then open
http://127.0.0.1:8000.

The first start creates two accounts (a 100,000 margin account and a 25,000 cash
account). State is saved in `backend/data/` and survives restarts.

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `PAPERDESK_SESSIONS` | `always` | `always`: the simulated market never closes. `us`: follow US market hours and holidays |
| `PAPERDESK_DATA` | `backend/data` | Where state is saved |
| `PAPERDESK_SEED` | `7` | Seed for the simulated price history |
| `PORT` | `8000` | Port (`run.sh` only) |

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
- Historical replay mode, chart drawing tools, Docker packaging, logins.

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
