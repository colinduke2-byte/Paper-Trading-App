# PaperDesk: handover and restart notes

Written 2026-11-24 about 05:10 UTC by the lead session, when the usage allowance was nearly spent.
Everything needed to resume is in this repository. Nothing depends on the lead session's memory.

## How to resume

1. Open a new lead session with this repository attached (the `paper-trading-app` environment has the
   data-provider hosts allowed) and tell it: "Read ops/HANDOFF.md on the `ops` branch and continue."
2. The old lead session (session_01ByytwY8Vod3giYjEJ9ytS9) may still be alive; if so, simply message it.
3. Rules for workers are in `CLAUDE.md` on `main`. Pull requests are opened with the REST command shown
   there (`gh pr create` does not work in cloud sessions). The lead merges only when all checks pass on
   GitHub and review findings are fixed.

## Where the work is

- `main` (fd85f99): everything merged so far. All checks green on its last commit except that the e2e
  job had not reported when last looked.
- `stream/*` branches: one per work stream (see table).
- `wip/<name>` branches: automatic snapshots of every local working copy INCLUDING uncommitted files,
  refreshed every 10 minutes while the lead's machine lived (`ops/snapshot.sh`). If a stream's own
  branch looks behind, its `wip/` branch has the latest files. To continue from one:
  `git checkout -b stream/<name>-resume origin/wip/<name>` (or cherry-pick its top commit).
- `ops/review-scripts/`: the independent reviewers' reproduction scripts (orders, lifecycle, release
  candidate). Paths inside them point at the old machine; adjust before running.
- Review reports that are not in the repository are summarised below; the UX review is being saved by
  its author as `docs/reviews/UX_REVIEW_2026-11-24.md` on `stream/uxpolish`.

## Merged to main

| PR | Stream | Notes |
|---|---|---|
| 1 | Packaging | Docker, CI on Linux/Windows/macOS, e2e suite, run scripts |
| 2 | Replay | Recorded sessions with clock control |
| 7 | Access | Optional LAN mode with passcode; security-reviewed, fixes applied |
| 9 | Combined: reports, live data (Alpaca/Finnhub), lifecycle, orders, phone layout | Orders and lifecycle independently reviewed; all fixes applied |
| 10 | Chart tools | Drawings, indicators, draggable order lines, shortcuts |
| 12 | Guide | User guide, quick start, known limits, in-app Help |

PRs 3, 4, 5, 6 and 8 were closed as superseded by PR 9.

## Open pull requests (none merged; all target main unless noted)

| PR | Branch | State when written | Next step |
|---|---|---|---|
| 13 | stream/lots | All 8 checks green (after a Windows time-conversion fix by the lead). Independent review running in cloud session session_01Byytwapga8V8MhMdzWYahT | Read the review; fix findings; merge |
| 14 | stream/crypto | Checks running. Independent review running in session_01ByytwYbp7KirUmqTTP1QTN | Same |
| 15 | stream/alerts | Checks running. 429 of 430 backend tests; the 1 failure is the cash-account bug below, not this branch | Merge after checks |
| 16 | stream/simresearch | Checks running | Merge after checks |
| 17 | stream/provideractions | Checks running. Touches engine/exchange (stale quotes, missing prices): deserves a review | Review, then merge |
| 18 | stream/ticketui | Just opened. Author reports e2e 100 passed, one flaky chart-tools test (see below) | Checks, then merge |
| 11 | stream/crossbrowser -> integration/wave1 | Opened early by its session; work was still in progress. Base must be changed to main | Wait for its report; retarget to main |

PRs 13 to 18 all edit the same core files (`exchange.py`, `App.tsx`, `Panels.tsx`, `Ticket.tsx`,
`README.md`). Do NOT merge them one by one without re-testing: start an integrator on a branch
`integration/wave2` from main that merges them in this order: 17 provideractions, 13 lots, 15 alerts,
16 simresearch, 14 crypto, 18 ticketui, then the fix branches below, writing interaction tests as the
wave-1 integrator did (see PR 9's description and comments for the pattern), and open one PR.

## Work in progress with no pull request yet

Local agents on the lead's machine (branches exist; latest files are on `wip/<name>`):

| Branch | Task | Brief summary |
|---|---|---|
| stream/threadsafety | RELEASE BLOCKER. One lock around ticks, mutating routes, mode switches and saves | Two reviewers reproduced `dictionary changed size during iteration` in the tick loop and in `_worst_case` under concurrent requests |
| stream/rcfixes | RELEASE BLOCKERS B2, B4 and release-review items | See "Release-candidate review" below |
| stream/cashfix | RELEASE BLOCKER. Cash account can end with negative cash | See "Confirmed money bug" below |
| stream/fixes | 10 bugs found by the guide writer | See list below |
| stream/uxpolish | Implementing the usability review | Saves its full report first; ticket items go to `docs/reviews/TICKET_HANDOFF.md` for the ticket stream |
| stream/soak | Long-run simulator, determinism audit, benchmarks, API fuzz (tests and tools only) | Findings go in `FINDINGS.md` on its branch |

Cloud sessions (each on its own machine; they push their branch when done and message the lead):

| Session id | Branch | Task |
|---|---|---|
| session_01ByytwYph1R8w3awZL43Qhf | stream/discovery | Watchlists, screener, movers, heatmap. Was running final test suites |
| session_01ByytwasdvqLUP3YCB8ntH4 | stream/backup | Backups, restore, migrations, never overwrite a damaged state file, data-directory lock |
| session_01ByytwbVSU1kpSY2fqiZB4j | stream/crossbrowser | WebKit/Firefox/Windows e2e in CI, accessibility checks, installable app |
| session_01ByytwYvYF7ikH9tiRUTe5a | stream/hardening | Whole-app security review fixes |
| session_01Byytwapga8V8MhMdzWYahT | (review only) | Review of PR 13 |
| session_01ByytwYbp7KirUmqTTP1QTN | (review only) | Review of PR 14 |

If a cloud session was reclaimed before pushing, its work is lost and the task must be re-run from the
brief (briefs are in the old lead session's transcript; the summaries here are enough to rewrite them).

## Release blockers still open on main

1. **Threading (B3).** Request threads and the tick loop mutate the exchange with no common lock.
   Owner: stream/threadsafety.
2. **Options settled at a zero price after a restart with the live feed (B2).**
   `exchange.py` `_process_expirations` used `spot = u.last if u else ZERO`; the Alpaca feed has no
   quotes until its first poll. Also `positions()` raises when an option's underlying has no quote
   (stops WebSocket ticks for everyone), and the first equity sample after start values unpriced
   positions at zero. Fix: settle at the underlying's close on the expiry date using
   `MarketDataProvider.closing_price(symbol, day)` (added on stream/provideractions). Owner: stream/rcfixes.
3. **Day orders never expire and Day P&L never resets if the app was closed at the day roll (B4).**
   `_last_day`/`_last_session` are not persisted. Owner: stream/rcfixes.
4. **Confirmed money bug: a cash account can end with negative cash.** Falsifying example for
   `tests/test_orders_review.py::test_cash_account_never_ends_an_expiry_with_negative_cash_or_short_stock`
   (fails on main fd85f99 when this example is used; a fresh random run usually passes):
   `steps=[('order', {'symbol': 'ABC', 'side': 'buy', 'qty': 100, 'type': 'limit', 'limit_price': '80', 'tif': 'gtc'}), ('order', {'symbol': 'ABC261127C00090000', 'side': 'buy', 'qty': 1}) x6], cash=10018, near_px=9001, far_px=6000`
   -> cash -5999.65. A resting GTC buy reserves 8,000; a long 90 call is exercised at expiry spending
   9,000 because the cash-account exercise limit `settle = min(n, cash // (strike*100))` counts
   reserved cash; later the resting buy fills at 60. Fix both ends: exercise may only spend unreserved
   cash (remainder closed for intrinsic value), and whenever cash drops outside the pre-trade check,
   resting orders that are no longer affordable are canceled with a reason. Widen the property test's
   generator to cover resting orders across expiries. Owner: stream/cashfix.
5. **Stale bundled UI (B1): fixed on main** by rebuilding `frontend/dist`; the automatic rebuild-on-main
   workflow and the staleness guard are part of stream/rcfixes. Until that lands, the lead must rebuild
   and commit `frontend/dist` on main after every merge that changes `frontend/src`
   (`cd frontend && npm install && npm run build`, commit `frontend/dist`).

## Review findings not yet fixed (summaries)

**Release-candidate review (verdict: ship after fixes), items assigned to stream/rcfixes unless noted:**
statements wrong after splits, reinvested dividends and "closed at expiry" (classify from ledger
postings; derive month-end positions from lots); equity includes pending dividends but not accrued
interest/borrow fees; interest memo quotes the rate at posting time; margin-call grace period counts
wall time while the app is closed or in replay; account reset drops the lot method and skips the
starting-cash cap; duplicate statistics implementation (`Exchange.performance` vs `analytics/`);
routes with no UI (lifecycle log, account delete); e2e servers should force `PAPERDESK_FEED=synthetic`;
no third-party notices file and the chart attribution link is not tappable on phones; backend
dependencies are unpinned; `Exchange.save` clears `dirty` before a successful write. Live-data items
went to stream/provideractions (done in PR 17): real expiries in validation, quote age, real splits.
Left for later: positions CSV and statement lack a price-source column.

**Whole-app security review (verdict: approve with fixes; nothing found for an unauthenticated network
device or a hostile web page), owner stream/hardening:** bound numbers once in `core/money.parse`
(extreme exponents freeze the server; a huge reset value bricks `/api/accounts` across restarts);
validate before replacing state in reset; WebSocket message validation and per-client isolation in
`broadcast`; request body and entity count limits; cap the option-symbol parser cache key length;
restrict news links to http/https; no redirects and bounded responses in `marketdata/http.py`;
validate and bound the research cache; bounded, type-checked replay session file reads; refuse
cross-site `/api` GETs, add `Cross-Origin-Resource-Policy`, disable `/docs` by default.

**Guide writer's bug list, owner stream/fixes:** `.env` values for `PAPERDESK_DATA`/`PAPERDESK_SEED`
ignored (read before `.env` is loaded); non-numeric starting cash returns HTTP 500; order rejection
leaks a Python exception name and buying-power rejections carry no amounts; Escape does not close most
dialogs; Reports/statements say "simulated" with real data on, and the default benchmark MKTX is
fictional; no on-screen notice when Alpaca is unreachable and the app falls back to simulated data and
the simulated accounts; chart "1M" button means one minute; Day P&L resets at UTC midnight in "us"
mode (three day-roll definitions exist: unify); the Alpaca check command prints stray lines; option
description formats differ; no UI to change a working order.

**Usability review:** ten main problems and 83 wording fixes; see the saved report on stream/uxpolish.
Headlines: a stopped server looks live; margin-call/liquidation/dividend/split events are never shown;
at 1280x720 the chart covers the bottom tabs; 900-1100 px collapses to one long column; the order
preview hides the x100 option multiplier and short-selling consequences; on phones Close/Cancel sit
off-screen; rejections do not say how to fix; times have no time zone; no confirmation on "cancel all"
or "exit replay".

**Ticket UI author's findings (PR 18):** preview text says "closeing price" (built from
`f"{which}ing"` in the backend); an OTO error names the API field `triggers_oco`; the chart-tools e2e
test "drawing tools create, select, move and delete shapes" is flaky on main (fails 1-2 runs in 4):
fix the test's waits, do not add retries.

## Known limits that only the owner can lift

- Nothing has run against Alpaca or Finnhub with a real key. The owner needs a free Alpaca paper
  account; keys go in `.env` next to `run.bat` (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, optional
  `FINNHUB_API_KEY`), then `python -m app.marketdata.alpaca --check` from `backend/`. See
  `docs/DATA_PROVIDERS.md`. The cloud environment's secure key form cannot hold Alpaca's two-header
  key, so live testing happens on the owner's PC.
- Nobody has run the app on a real Windows PC (only GitHub's Windows machines) or a real phone (only
  Chromium emulation; WebKit runs are part of stream/crossbrowser).
- The repository has no LICENSE for the owner's own code (owner's decision; only matters if public).

## Decisions made on the owner's behalf (reversible)

- Data provider: Alpaca free plan (IEX real-time, indicative options), Finnhub optional.
- Cash accounts cannot hold credit spreads or calendars; a long option is not cover there.
- Dividends cannot be switched off per account (the price drops on the ex-date regardless).
- An in-the-money option a cash account cannot afford to exercise is closed for its cash value.
- The pattern day trader rule never blocks a closing order; it blocks new opening orders after three
  day trades.
- Phone access: optional same-Wi-Fi mode with a passcode (plain HTTP; home networks only).
- Crypto was included because the owner said "do everything".

## Remaining backlog after the above

Combine and merge PRs 13-18 and the fix branches; a second release-candidate review of the result;
update the user guide and in-app Help for the new order types, strategy builder, lots, alerts, crypto
and research; specific follow-ups listed in each PR's "descoped" section; tag a release only with the
owner's go.
