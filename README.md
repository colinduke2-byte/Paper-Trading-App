# PaperDesk

A paper trading simulator: practise trading stocks and options with pretend money. It runs on your own
computer, in your browser (and a phone browser on the same Wi-Fi), and needs no account, API key or
internet connection to start.

PaperDesk **never sends an order to a broker**. By default **all market data is simulated**: the companies
are fictional and their prices follow a random walk; option prices come from a pricing model. With a free
Alpaca key it can show real stock and option quotes (see below), and orders are still only simulated.
Nothing here is investment advice.

## Documentation

| Read | For |
|---|---|
| [docs/QUICK_START.md](docs/QUICK_START.md) | One page: from nothing to a first paper trade on Windows, then phone access |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | The full guide: install, the screen, orders, how fills work, accounts and margin, options, reports, replay, phone access, real data, troubleshooting, glossary |
| [docs/KNOWN_LIMITS.md](docs/KNOWN_LIMITS.md) | What is simulated, approximate, untested or not built |
| [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md) | Alpaca and Finnhub: facts, setup, how the adapter works |
| [docs/REPLAY.md](docs/REPLAY.md) | Historical replay and the session file format |
| [docs/PLAN.md](docs/PLAN.md) | The original plan (not a description of what exists) |

The app also has a **Help** button with the essentials, for use offline.

## Run it

Requires Python 3.11 or newer (Node is not needed).

```bash
./run.sh
```

On Windows, double-click `run.bat` (it tries `py -3`, then `python`, and opens your browser once the
server answers; the Windows path is checked by CI but has not been tried by hand). Then open
http://127.0.0.1:8000. The first start creates a virtual environment and installs from
`backend/pyproject.toml`; later starts reuse it until `pyproject.toml` changes.

The first start creates two accounts (a 100,000 margin account and a 25,000 cash account). State is saved in
`backend/data/` and survives restarts.

> **By default there is no login.** Anyone who can reach the port can trade and delete your paper accounts,
> so PaperDesk listens on `127.0.0.1` only. To use it from your phone, turn on LAN mode (below), which adds a
> passcode. Never expose PaperDesk to the internet.

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `PAPERDESK_SESSIONS` | `always` (`us` with real data) | `always`: the simulated market never closes. `us`: follow US market hours and holidays |
| `PAPERDESK_DATA` | `backend/data` | Where state is saved. Set it in the environment: the server ignores it in `.env` |
| `PAPERDESK_SEED` | `7` | Seed for the simulated price history. Set it in the environment, not `.env` |
| `PORT` | `8000` | Port (`run.sh`, `run.bat`, Docker). In `.env` it is read by Docker only |
| `HOST` | `127.0.0.1` | Bind address (`run.sh`, `run.bat`; `0.0.0.0` in LAN mode). A network address is refused without LAN mode. Only the Docker image sets `0.0.0.0`, inside the container |
| `PAPERDESK_LAN` | `0` | `1`: let devices on your network connect (needs `PAPERDESK_PASSCODE`). See LAN mode |
| `PAPERDESK_PASSCODE` | unset | Passcode, at least 8 characters. When set, browsers must sign in |
| `PAPERDESK_SESSION_DAYS` | `30` | Sign-ins expire after this many days without use |
| `PAPERDESK_LOOPBACK_BYPASS` | `1` | `1`: browsers on this computer skip the passcode. `0`: everyone signs in |
| `PAPERDESK_ALLOWED_HOSTS` | unset | Extra host names to answer to besides `localhost` (for example `mypc.local`); in LAN mode IP addresses always work |
| `PAPERDESK_BIND` | `127.0.0.1` | Docker only: where compose publishes the port. `0.0.0.0` is refused without LAN mode |
| `PAPERDESK_FEED`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `FINNHUB_API_KEY`, ... | unset | Real market data: see [docs/DATA_PROVIDERS.md](docs/DATA_PROVIDERS.md) |

`.env.example` lists every setting. Each can go in the environment or in `.env` next to `run.sh` (read first, at
start-up, by the server and by `run.sh`/`run.bat`; a variable already set in the environment wins).

### Use it from your phone (LAN mode)

Step-by-step instructions for Windows are in [docs/USER_GUIDE.md](docs/USER_GUIDE.md) (section 12) and
[docs/QUICK_START.md](docs/QUICK_START.md). In short: copy `.env.example` to `.env` (next to `run.sh`), set

```bash
PAPERDESK_LAN=1
PAPERDESK_PASSCODE='a long passcode you use nowhere else'
```

and start `run.sh` or `run.bat`. PaperDesk listens on all network interfaces and prints the address to open on
the phone, such as `http://192.168.1.20:8000`. The phone must be on the same Wi-Fi. On Windows, allow Python
through the firewall on **Private networks only**.

**What this protects, honestly.** LAN mode uses plain HTTP, not HTTPS: anyone on the same network could watch
the traffic, including the passcode and the session cookie. Use it only on your own home network, never on
public or shared Wi-Fi, and do not reuse the passcode. HTTPS with a self-signed certificate is not included.
Passcodes that are the example text, a common word or number sequence, or have fewer than 4 different
characters are refused at start-up.

Mechanics, for developers and reviewers:

- Sign-ins last `PAPERDESK_SESSION_DAYS` days without use and survive a restart: only a SHA-256 digest of each
  session token is stored, in `sessions.json` in the data directory (owner-only permissions on Linux and
  macOS). Changing the passcode, or deleting `sessions.json`, signs every device out.
- Wrong passcodes are slowed per device (after three tries, 1, 2, 4 ... seconds, up to 15 minutes) and all
  sign-ins pause after 30 failures in 15 minutes; restarting clears the counters. Failed attempts are logged
  with the device address, never with what was typed. The passcode and session tokens are never logged.
- The session cookie is `HttpOnly` and `SameSite=Strict`; requests that change anything, and the live stream,
  are refused when they come from another website; the server only answers to `localhost`, `127.0.0.1`, IP
  addresses (LAN mode) and `PAPERDESK_ALLOWED_HOSTS`, which blocks DNS rebinding; responses carry a
  Content-Security-Policy and related headers.
- A reverse proxy in front of PaperDesk is not supported: the server ignores `X-Forwarded-For` (uvicorn runs
  with `--no-proxy-headers`) and never lets a request that carries proxy headers skip the passcode. If you use
  one anyway, set `PAPERDESK_LOOPBACK_BYPASS=0`.
- Browsers do not separate cookies by port, so the session cookie also reaches other web apps on the same
  address. Under Docker Desktop all devices share one wrong-passcode counter.
- Server-rendered pages cannot use inline scripts under the Content-Security-Policy. A print button is written
  as `<button type="button" data-print>Print</button><script src="/print.js"></script>`.

### Docker

```bash
cp .env.example .env     # optional
docker compose up --build
```

Then open http://127.0.0.1:8000. State lives in the named volume `paperdesk-data` (`docker compose down -v`
deletes it). The compose file publishes the port on `127.0.0.1` only, which is what keeps the unauthenticated app
private; do not edit that line. LAN mode in Docker: put `PAPERDESK_LAN=1`, `PAPERDESK_PASSCODE='...'` and
`PAPERDESK_BIND=0.0.0.0` in `.env`; the server refuses to start if `PAPERDESK_BIND` points at the network without
LAN mode and a passcode. In Docker every browser has to sign in, including the one on the computer (compose sets
`PAPERDESK_LOOPBACK_BYPASS=0`).

## What works

- **Market data:** simulated prices for fictional companies by default. With Alpaca keys the app uses real
  stock and option quotes (IEX real time on the free plan; options are the indicative feed), with Finnhub
  optional for earnings and fundamentals; it falls back to simulated prices and says why. The live adapter
  has not yet been run with a real key. Every quote is labelled with its source; option prices without
  provider data are model prices.
- **Market views:** watchlist with search, level 1 quotes, candlestick charts (1 minute to weekly) with SMA,
  EMA, Bollinger bands, VWAP and RSI, a synthetic depth ladder, time and sales, company facts, screener, and a
  Research tab (real data needs a provider).
- **Orders in the ticket:** market, limit, stop, stop limit and trailing stop; day, good till canceled,
  immediate or cancel and fill or kill; a bracket (take profit and stop loss); fractional shares; order
  preview; cancel. **In the engine and API, not yet in the ticket:** market and limit on open and on close,
  good-till-date, one-cancels-other and one-triggers-other groups, multi-leg orders and changing an order.
- **Options:** chains with weekly and monthly expiries, implied volatility and greeks, single-leg trades from
  the chain; automatic exercise and assignment at expiry; strategy-aware margin for spreads, condors,
  butterflies and calendars (multi-leg orders are API-only for now).
- **Accounts:** cash and margin accounts, several at once, reset; Reg T style margin and buying power, short
  selling, margin calls with liquidation after a grace period. Per-account settings (Settings button) for
  dividend reinvestment, T+1 settlement and the settled-cash rule, the pattern day trader rule, margin interest
  and short borrow fees with their rates.
- **Corporate actions:** splits (lots, options and open orders adjusted or canceled, cash in lieu) and
  dividends, for the simulated feed. Real-provider actions are shown in Research but not applied.
- **Records and reports:** double-entry ledger, order and fill history, tax lots with FIFO, LIFO or highest-cost
  matching, realised gains report with holding period and an informational wash-sale flag, monthly statements,
  CSV export, trade journal, performance statistics (Reports tab), price alerts.
- **Historical replay:** replay a recorded or bundled session in a sandbox account with a simulated clock
  (`docs/REPLAY.md`); live state is never touched.
- **Help:** an in-app Help panel (top bar, or the menu on a phone).

## What is not built yet

See `docs/KNOWN_LIMITS.md`. Still to do: ticket screens for the engine-only order types, chart drawing tools,
early assignment and American-style pricing, corporate actions from a real provider applied to accounts, user
accounts and HTTPS.

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
starts `run.bat` and checks that the API and UI answer; and a Docker job that builds the image, runs it and checks
that the API and UI answer. The e2e run starts a second server with a passcode (loopback bypass off) for the
sign-in tests (`e2e/tests/auth.spec.ts`, desktop and Pixel 7; `E2E_AUTH_PORT`, `E2E_PASSCODE`).

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
