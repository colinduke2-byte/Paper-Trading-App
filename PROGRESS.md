# Progress: stream/backup (durable, portable user data)

PR: https://github.com/colinduke2-byte/Paper-Trading-App/pull/19 (base main, not merged)

## Done
- Recovery on damaged or newer state (copy to recovery/, saving off, recovery screen: restore / fresh / quit)
- Visible save failures with retry and back-off; one server per data folder (OS lock, stale-lock rule)
- Schema v2 with ordered migrations, v1 fixtures from old commits, forward refusal
- Segment (append-only) saves: 11.4 MB to about 0.47 MB per save at 10k fills; fault-injection and kill -9 tests
- Rotating verified backups and pre-action snapshots; zip archive download and restore with a dry run; account export and import
- Data and backups UI (desktop and phone); docs/STATE_FORMAT.md
- Tests after merging main (2dd82f5): backend 686 passed, frontend vitest 43 passed, e2e 108 passed / 4 skipped
  (2 on purpose: the data phone tests run on Pixel 7 only; 2 come from main)

## In progress
- Nothing. All eight findings of the independent review (PR comment) are fixed, each with a regression test in
  backend/tests/test_storage_review.py. Waiting for re-review.

## Next (descoped)
- An index for finished orders, so state.json stops growing with order history
- Restore one account from a full backup

## Known failing tests
- None on this branch

# stream/discovery progress

## Done
- Backend `app/discovery/` (store, stats, data, screener, search) and `app/api/routes_discovery.py`:
  named watchlists (create, rename, duplicate, delete, reorder, add, layouts), ranked search and recent symbols,
  screener (filters, presets, saved screens), `/api/markets/overview`, `/api/compare`.
- Live-feed budget: universe snapshot scan of at most 20 requests a minute, history for at most 20 symbols a minute.
  The watchlist on screen is declared to the feed (`marketdata/config.py` `wanted_symbols`, a small change).
- UI: `Watchlists.tsx`, `SymbolSearch.tsx`, `Screener.tsx` (replaces the old one in Panels.tsx), `Markets.tsx`
  (main view tab, heatmap plus a ranked list on phones), `Compare.tsx`, `Spark.tsx`, `discovery.css`; registered in App.tsx.
- Tests: `backend/tests/test_discovery.py` (22 pass), `e2e/tests/discovery.spec.ts` and `discovery.mobile.spec.ts`
  (6 pass on desktop, iPhone 13 and Pixel 7).

## State
- origin/main merged (App.tsx conflict resolved). Backend 419 passed. Full e2e: 86 passed and 2 failed on the first run.
  The Screener failure was fixed: a Sector picker is back in the screener toolbar. "option chain: buy one call" then passed twice;
  it looks intermittent and is outside this stream. After the fix: desktop app+discovery 17/17 (twice), phones 44/44.
- PR to main opened; see the lead message.

## Known failing
- tests/test_orders_review.py::test_requirement_is_the_minimum_over_all_valid_pairings (known, fixed elsewhere).
