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

> **By default there is no login.** Anyone who can reach the port can trade and delete your paper accounts,
> so PaperDesk listens on `127.0.0.1` only. To use it from your phone, turn on LAN mode (below), which adds a
> passcode. Never expose PaperDesk to the internet.

| Setting (environment variable) | Default | Meaning |
|---|---|---|
| `PAPERDESK_SESSIONS` | `always` | `always`: the simulated market never closes. `us`: follow US market hours and holidays |
| `PAPERDESK_DATA` | `backend/data` | Where state is saved |
| `PAPERDESK_SEED` | `7` | Seed for the simulated price history |
| `PORT` | `8000` | Port (`run.sh`, `run.bat`, Docker) |
| `HOST` | `127.0.0.1` | Bind address (`run.sh`, `run.bat`; `0.0.0.0` in LAN mode). A network address is refused without LAN mode. Only the Docker image sets `0.0.0.0`, inside the container |
| `PAPERDESK_LAN` | `0` | `1`: let devices on your network connect (needs `PAPERDESK_PASSCODE`). See LAN mode |
| `PAPERDESK_PASSCODE` | unset | Passcode, at least 8 characters. When set, browsers must sign in |
| `PAPERDESK_SESSION_DAYS` | `30` | Sign-ins expire after this many days without use |
| `PAPERDESK_LOOPBACK_BYPASS` | `1` | `1`: browsers on this computer skip the passcode. `0`: everyone signs in |
| `PAPERDESK_ALLOWED_HOSTS` | unset | Extra host names to answer to besides `localhost` (for example `mypc.local`); in LAN mode IP addresses always work |
| `PAPERDESK_BIND` | `127.0.0.1` | Docker only: where compose publishes the port. `0.0.0.0` is refused without LAN mode |

`.env.example` lists every setting, including reserved names for the market data work.

### Use it from your phone (LAN mode)

LAN mode is off by default. To turn it on, copy `.env.example` to `.env` (next to `run.sh`) and set:

```bash
PAPERDESK_LAN=1
PAPERDESK_PASSCODE='a long passcode you use nowhere else'
```

(or set the same variables in the environment). Then start `run.sh` or `run.bat` as usual. PaperDesk now
listens on all network interfaces and prints the address to open on your phone, such as
`http://192.168.1.20:8000`. The phone must be on the same Wi-Fi network as the computer. On Windows the
firewall may ask whether to allow Python: allow it on **Private networks only**, never Public. If the phone
cannot connect, check that Windows calls your Wi-Fi a private network.

The phone shows a sign-in screen; after the passcode it stays signed in for 30 days of no use
(`PAPERDESK_SESSION_DAYS`). Use "Sign out" in the top bar to end the session on that device. Browsers on the
computer itself skip the passcode unless `PAPERDESK_LOOPBACK_BYPASS=0`. Sign-ins survive a restart: only a
SHA-256 digest of each session token is stored, in `sessions.json` in the data directory (owner-only file
permissions on Linux and macOS). Changing the passcode signs every device out; deleting `sessions.json` does
too. Without LAN mode or a passcode the program behaves as before: `127.0.0.1` only, no login.

**Be honest with yourself about what this protects.** LAN mode uses plain HTTP, not HTTPS: anyone on the same
network could watch the traffic, including the passcode as you type it and the session cookie, and could then
use your session. Use it only on your own home network, never on public or shared Wi-Fi (cafes, hotels,
offices, student housing). It is paper money, but do not reuse the passcode anywhere else. HTTPS with a
self-signed certificate is not included: phones show alarming warnings for such certificates and they are
hard to set up well, so it was left out rather than done badly.

Passcodes that are the example text from `.env.example`, a common word or number sequence, or have fewer than
4 different characters are refused at start-up.

Trade-offs to know about:

- If the sign-in screen says there were too many wrong passcodes and it was not you, someone on your network
  may be guessing. Restarting PaperDesk clears the counters; then change the passcode.
- Under Docker Desktop (and other setups where Docker forwards the port) every device reaches the container
  from the same address, so they share one counter: wrong guesses on one device slow down the others.
- Browsers do not separate cookies by port, so the session cookie is also sent to any other web app on the
  same address (for example another program on `http://192.168.1.20:3000`). Do not run untrusted web apps on
  the same computer while LAN mode is on.

What else is in place: wrong passcodes are slowed down per device (after three tries, 1, 2, 4 ... seconds, up
to 15 minutes) and all sign-ins pause for a while after 30 failures in 15 minutes; failed attempts are logged
with the device address, never with what was typed. The passcode and session tokens are never logged. The
session cookie is `HttpOnly` and `SameSite=Strict`; requests that change anything, and the live stream, are
refused when they come from another website; the server only answers to `localhost`, `127.0.0.1`, IP
addresses (LAN mode) and `PAPERDESK_ALLOWED_HOSTS`, which blocks DNS rebinding attacks; and responses carry
a Content-Security-Policy and related headers. A reverse proxy in front of PaperDesk is not supported in this
build: the server ignores `X-Forwarded-For` (uvicorn runs with `--no-proxy-headers`) and never lets a request
that carries proxy headers skip the passcode. If you use one anyway, set `PAPERDESK_LOOPBACK_BYPASS=0`.

Server-rendered pages cannot use inline scripts under the Content-Security-Policy. A print button is written
as `<button type="button" data-print>Print</button><script src="/print.js"></script>`.

### Docker

```bash
cp .env.example .env     # optional
docker compose up --build
```

Then open http://127.0.0.1:8000. State lives in the named volume `paperdesk-data`
(`docker compose down -v` deletes it). The compose file publishes the port on `127.0.0.1` only, which is
what keeps the unauthenticated app private; do not edit that line. CI builds the image and checks that it
starts and answers.

LAN mode in Docker: put `PAPERDESK_LAN=1`, `PAPERDESK_PASSCODE='...'` and `PAPERDESK_BIND=0.0.0.0` in `.env`,
then `docker compose up --build`. Compose then publishes the port on all interfaces of the computer; the
server refuses to start if `PAPERDESK_BIND` points at the network without LAN mode and a passcode. Open
`http://<this computer's address>:8000` on the phone (find the address with `ipconfig` on Windows or
`ip addr` on Linux). In Docker every browser has to sign in, including the one on the computer, because
requests reach the container through Docker's network rather than from `127.0.0.1` (compose sets
`PAPERDESK_LOOPBACK_BYPASS=0` for this reason).

## What works

- **Market data:** simulated prices for fictional companies by default. With Alpaca keys the app
  uses real stock and option quotes (IEX real time on the free plan), with Finnhub optional for
  earnings and fundamentals; it falls back to simulated prices and says why. Setup:
  `docs/DATA_PROVIDERS.md`. Every quote is labelled with its source; option prices without provider
  data are model prices.
- **Market views:** watchlist with search, level 1 quotes, candlestick charts (1 minute
  to weekly) with SMA, EMA, Bollinger bands, VWAP and RSI, depth ladder, time and sales,
  company facts, screener, and a Research tab (news, earnings and dividend calendar,
  corporate actions, fundamentals). Without a provider the Research tab is filled with simulated
  research for the fictional companies: financial statements, earnings reports that gap the price,
  news, and an economic calendar, all labelled Simulated (see `docs/SIMULATED_RESEARCH.md`).
- **Orders:** market, limit, stop, stop-limit and trailing stop; market and limit on open
  and on close; day, good-till-canceled, good-till-date, immediate-or-cancel and
  fill-or-kill; brackets, one-cancels-other groups and one-triggers-other chains; order
  preview, change and cancel; fractional shares.
- **Options:** chains with weekly and monthly expiries, implied volatility and greeks,
  single-leg trades and multi-leg orders (two to four legs, all-or-none at a net price)
  with payoff preview and strategy-aware margin for spreads, condors, butterflies and
  calendars; automatic exercise and assignment at expiry.
- **Accounts:** cash and margin accounts, several at once, reset; Reg T style margin and
  buying power, short selling. Per-account settings (Settings button) for dividend
  reinvestment, T+1 settlement and the settled-cash rule, the pattern day trader rule,
  margin interest and short borrow fees with their rates, and margin-call liquidation
  after a grace period.
- **Corporate actions:** splits (lots, options and open orders adjusted or canceled, cash
  in lieu) and dividends; the simulated feed schedules them for its companies.
- **Records and reports:** double-entry ledger, order and fill history, tax lots with FIFO,
  LIFO or highest-cost matching, realised gains report with holding period and
  wash-sale flag, monthly statements, CSV export, trade journal, performance analytics
  (Reports tab), price alerts.
- **Historical replay:** replay a recorded or bundled session in a sandbox account with a
  simulated clock (`docs/REPLAY.md`); live state is never touched.

## What is not built yet

See `docs/PLAN.md` for the full plan. Still to do:

- Corporate actions from a real provider (Alpaca's are shown in Research but not applied
  to accounts; only the simulated feed's are).
- Early assignment, American-style pricing.
- Chart drawing tools, user accounts (LAN mode has a single shared passcode), HTTPS.

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
