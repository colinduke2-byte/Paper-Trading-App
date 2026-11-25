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

## Updates after the first version of this note

- 05:07 UTC: PR 15 (alerts and notification centre) MERGED to main; bundled UI rebuilt (main at 8934a60).
  All 8 checks were green on the PR head.
- PR 14 (crypto): all 8 checks green; waiting only for its independent review
  (session_01ByytwYbp7KirUmqTTP1QTN).
- PR 19 (stream/backup -> main, head 9a03465) opened: state format v2 with append-only segment files,
  rotating backups, download/restore, migrations with fixtures from earlier builds, data-directory
  lock, recovery mode instead of overwriting a damaged state file. Author reports 480 backend tests and
  88 e2e. For the thread-safety work: `ex.save_plan()` is pure serialisation (call under the lock) and
  `ex.write_plan(plan)` is I/O only (call outside it). Not yet reviewed; it reworks persistence, so it
  needs an independent review before merging. Its Windows paths (msvcrt lock) only run in CI.
- PR 13 (lots) review finished: APPROVE WITH FIXES, no ledger corruption. Findings sent to the lots
  session (session_01ByytwcAyVLJZvZxFGivyho) to fix: (1) `_cancel_broken_lot_orders` checks each order
  alone, so two orders naming parts of one lot can over-reserve it and one can open a short;
  (2) same-tick race only partly fixed: a plain sell entered first can take a named lot and the lot
  order then opens a short or closes unnamed lots (book lot-order fills first within a batch and never
  fill beyond what the named lots hold); (3) wash sale can use a replacement share twice
  (`analytics/wash.py` ~158); (4) wash analysis is quadratic and runs on every lots/gains request;
  (5) huge lot quantity returns HTTP 500; (6) `"lots": []` silently ignored; (7) Ticket.tsx sums lot
  quantities as floats. Merge PR 13 only after these are fixed.
- PR 18 (ticket UI) opened by its author; see the open pull request table.
- Since alerts merged, PRs 13, 14, 16, 17, 18 and 19 each need main merged in again (or go through
  the `integration/wave2` integrator as described above; add 19 backup after 17 in the order).
- PR 16 (simulated research): all 8 checks green at 4a6fb0c; its session was asked to merge the new
  main (alerts) and push so checks re-run on the real combination before merging.
- PR 14 (crypto) review finished: APPROVE WITH FIXES; stocks and options byte-identical to main.
  Findings sent to the crypto session (session_01Byytwa7UbHxm5YYWZw1VQb): (1) HIGH: a crypto stop
  (market) buy can drive cash negative in cash and margin accounts (stop 41,000, gap to 60,000 ->
  cash -44,360): reject stop-buy without a limit AND cap any crypto buy fill to affordable cash;
  (2) raising `crypto_fee_rate` with a resting buy makes cash negative; (3) the "resting crypto buy
  blocks a margin stock buy" rule is order-dependent and buying power does not reflect it: apply it
  only to crypto buys; (4) liquidation sells crypto in whole coins (lifecycle/manager.py ~582);
  (5) provider sizes not floored to the increment leave unsellable dust; (6) `symbols()` copies the
  asset table on every call; plus low items. Merge PR 14 only after these are fixed.
- Pre-existing nondeterminism noted by that reviewer: `exchange.py` ~1190 iterates a set of symbols,
  so same-tick option fill order varies with the hash seed. Make it sorted (deterministic engine rule).
- PR 17 (provider actions): all 8 checks green at db574d0 (against main before alerts). Independent
  review started in session_01ByytwaR9ENg7JRY7d7YiUJ. Needs main merged in.
- PR 19 (backup): independent review started in session_01ByytwbsKMUxhNjqt2iRG1y.
- PR 20 (stream/discovery -> main, head 0f93dce) opened: multiple watchlists with columns, screener
  with saved screens, Markets view with movers and a sector heatmap, symbol search, compare. Author
  reports 419 backend tests; e2e 86 passed with one intermittent failure outside its area.
- Flaky e2e tests to fix (fix the waits; do not add retries): "option chain: buy one call" (seen by
  three different agents: the toast says "Order placed" and no position appears, i.e. the limit order
  taken from the chain's ask sometimes rests because the model price moved; make the test robust or
  make the chain click use a marketable price) and the chart-tools test "drawing tools create, select,
  move and delete shapes" (fails 1-2 runs in 4 on main).
- Several cloud sessions had `git commit` refused by the automatic permission check at first and it
  worked on a later attempt (alerts, simulated research, discovery). If a session reports being
  blocked on commit, it is that; it has resolved itself each time so far.
- PR 16 (simulated research, with earnings alerts through the notification centre) MERGED to main; bundled UI rebuilt. All 8 checks were green on the combination with alerts.

## STATE AT HAND-OVER TO A NEW LEAD SESSION (2026-11-25 about 00:55 UTC) — read this section first

Follow `ops/PLAN_TO_FINISH.md`. Owner's instructions since: finish the project; be economical with
tokens; NO new Fable agents (use Opus for reviews and building, Sonnet for mechanical work); at most 4
workers at a time; report to the owner only at merges, decisions and phase boundaries; the deferred
list in the plan is accepted.

### main is at 732c240 and now also contains
PR 15 alerts and notification centre; PR 16 simulated research (statements, earnings gaps, news,
calendar, earnings alerts); PR 24 fixes (single `.env` loader in `core/envfile.py`; `money.parse(x,
what)` raising ValueError with field names; `core/errors.NotFound` instead of a global KeyError
handler; `tests/test_api_fuzz.py` requires a baseline body for every non-GET route; shared
`frontend/src/Dialog.tsx`; `calendar.trading_day`/`trading_day_start` as the one day definition;
synthetic feed starts with 1,500 minutes of history; chain clicks seed a marketable limit;
"Change..." on working orders; feed-derived simulated/real labels and default benchmark; fallback
notice when Alpaca is unreachable). The two flaky e2e tests are fixed there.
After each merge that changes `frontend/src` the lead rebuilds and commits `frontend/dist` on main
(`cd frontend && npm install && npm run build`), until PR 23's auto-rebuild workflow is merged.

### Phase 1 (release blockers): status
| PR | Branch | Status | Next step |
|---|---|---|---|
| 23 | stream/rcfixes | B2 (expiry at zero price) and B4 (day orders after restart) fixed plus the release-review items. Was green except the midnight test (now fixed on main). Its author is merging main into it on the old lead's machine and will push. | When its head is newer than 19cf92c and checks are green: merge. If no new push appears, merge origin/main into it yourself (keep main's `trading_day` line in `_roll_sessions` with its `_settle_day_start` body; preflight stdout is now "host port"; use the shared Dialog; add fuzz baselines) and lower the default margin-call grace to 13 hours of market-open time. |
| 26 | stream/threadsafety | One lock (`ctx.lock`) around tick, routes and saves; rollback on failed order actions; 506 backend tests. A review is in progress and will be posted as a comment on the PR. | Read the PR comment; have fixes made (Opus); merge main in; merge when green. Known follow-ups it listed: alerts engine fetches bars under the lock (must not, with a live feed); `/api/fundamentals` 500 when a live quote lacks day high/low. |
| 28 | stream/cashfix | Cash-account negative-cash bug fixed (three root causes); touches only exchange.py and test_orders_review.py; 487 backend tests under 4 seeds. A review is in progress and will be posted as a comment on the PR. | Read the PR comment; fixes if any; merge main in; merge when green. |

### Phase 2 (review fixes): nothing has been resumed yet
- PR 13 lots: 7 review findings listed earlier in this file; the author's session
  (session_01ByytwcAyVLJZvZxFGivyho) stopped at the usage limit, head 1d525cd may contain part of
  the fixes. Check what is done against the list; finish the rest.
- PR 14 crypto: findings listed earlier (the HIGH one: stop-market buy can overdraw); author session
  session_01Byytwa7UbHxm5YYWZw1VQb stopped at the limit, head cf767bc. Same approach.
- PR 17 provider actions and PR 19 backup: their Fable reviews were cut off by the limit and produced
  nothing. Re-run each as ONE Opus review that posts its findings on the PR. Briefs for both are
  summarised in the "Open pull requests" table and the updates above (17: stale-quote and empty-side
  rules must not change simulated-feed behaviour; day roll must not stall; exactly-once corporate
  actions. 19: crash consistency of segment files, the append-only assumption versus features that
  rewrite history, migration fixtures, lock, restore safety).
- stream/hardening: its cloud session (session_01ByytwYvYF7ikH9tiRUTe5a) never pushed a branch;
  treat its work as lost unless that session can be resumed. PR 24 already covers part of its list
  (non-numeric input 500s, WebSocket account validation, fuzz test). Remaining from the security
  review: number magnitude/exponent bounds in `money.parse`, validation before state replacement in
  reset, request body and entity count limits, symbol length cap before the option-symbol cache,
  news links http/https only, no redirects and bounded responses in `marketdata/http.py`, research
  cache bounds, bounded replay session file reads, CORP header and no cross-site `/api` GETs,
  `/docs` off by default.
- stream/crossbrowser (PR 11, head a663f20) and stream/uxpolish (head a4edc16 plus later snapshot on
  wip/uxpolish; its full review is saved as docs/reviews/UX_REVIEW_2026-11-24.md on that branch) and
  stream/soak (only on wip/soak) were cut off mid-work.

### Phases 3 to 7: as in ops/PLAN_TO_FINISH.md
Integrator order for `integration/wave2` from main: 17, 19, 13, 14, 18, 20, then hardening,
crossbrowser, uxpolish. Then one Opus release review, docs update (Sonnet), owner testing, tag.

### Practical notes for the new lead
- Local agents and `wip/*` snapshots belong to the old lead's machine; a new session cannot resume
  those agents. Work from the pushed branches (`stream/*`), using `wip/<name>` only to recover
  unpushed files (`git checkout -b <new> origin/wip/<name>`).
- Cloud worker sessions can be messaged by id with send_message if they still exist.
- `gh pr create` does not work; use the REST form in CLAUDE.md. Merge with
  `gh api -X PUT repos/colinduke2-byte/paper-trading-app/pulls/<n>/merge -f merge_method=merge -f sha=<head>`.
- Check results: `gh api repos/colinduke2-byte/paper-trading-app/commits/<sha>/check-runs`.
- Owner still needs to: create Alpaca paper keys; test on a real Windows PC and phone (phase 6);
  decide on a LICENSE only if the repository ever goes public.
- UPDATE 01:10 UTC: PR 23 pushed, head ed44cd8, main merged in, grace default now 13 hours. Author reports backend 581 passed, e2e 101 passed with 1 failure it calls a click race (chart.spec.ts:460, the replay drawing test; passed twice alone: a third flaky test to fix properly, no retries). Next step: wait for checks on ed44cd8 and merge if green.

## NEW LEAD: session_01ByytwXuyEEXyEihpExRGHy (from 2026-11-25 00:55 UTC)
- Old lead (session_01ByytwY8Vod3giYjEJ9ytS9) retired: starts nothing, merges nothing. Its two local
  reviews (PR 26, PR 28) post as PR comments, expected by about 01:15; if absent by 01:45, re-run on Opus.
- Owner instruction: keep going through PLAN_TO_FINISH.md without waiting between steps; stop only
  for the owner's decisions; keep this file current.
- PR 21 (cash exercise vs later short put) is superseded by PR 28; PR 27 (synthetic prior-day bars)
  is superseded by main's HISTORY_MINUTES=1500. Closing them was refused by the permission check:
  owner to close.
- PR 22 (fundamentals 500) and PR 28 updated with main (GitHub update-branch) to re-run checks.
- PR 25 (alerts off the lock) carries stream/threadsafety: merge after PR 26, then update.
- Phase 2 resumed: lots author session_01ByytwcAyVLJZvZxFGivyho (PR 13) and crypto author
  session_01Byytwa7UbHxm5YYWZw1VQb (PR 14) asked to finish fix lists, merge main, comment per finding.
  Opus review of PR 17: session_01ByytwYofFEfccUETS1aWHu (posts on the PR). PR 19 review next.
- 01:12 UTC: PR 23 MERGED (main 509a043); the ui-dist workflow now rebuilds frontend/dist on main
  automatically (no manual rebuilds). PR 13 lots fixed at 7d1c23f and PR 14 crypto fixed at 1211fff
  (each with a per-finding PR comment): ready for the integrator.
- PR 26 review: approve with fixes. PR 25's session declined new scope, so worker
  session_01ByytwYu2xXB35KoQk8wvu6 builds `stream/threadsafety-final` from PR 25 + review fixes and
  opens one PR superseding 25 and 26.
- PR 28 review: SEND BACK, seven blocking items (comment on PR 28). Worker
  session_01Byytwbmbp2zVBAAd9s3p93 fixes them on stream/cashfix. Cash invariant still open on main.
- PR 19 review: session_01ByytwYu2DnD9MJeK4CGJKQ. PR 22 waiting for green checks to merge.
- PRs 21 and 27 CLOSED by the owner as superseded (by PR 28 and by main).
- 01:26 UTC: PR 22 (fundamentals with no day range) MERGED.
- PR 17 review: REJECT until findings 1-3 fixed (late corporate action applied to a position bought
  after the ex-date; margin calls can be switched off indefinitely; simulated-feed fills change).
  Fix worker session_01ByytwbfYBDGd6t6MFSgJ37 on stream/provideractions.
- PR 19 review: approve with fixes (HIGH: reused order id after a crash loads the lost order).
  Sent to its author session_01ByytwasdvqLUP3YCB8ntH4.
- Workers now: threadsafety-final, cashfix, provideractions fixes, backup fixes.
- Still to schedule: hardening redo (list above) and crossbrowser/uxpolish completion feed into phase 3.
