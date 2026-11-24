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
