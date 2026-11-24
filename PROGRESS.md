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

## In progress
- A full backend and e2e run on this commit. The earlier e2e run was invalid: frontend/dist was restored mid-run.
- Merge origin/main (9 commits ahead; App.tsx and mobile.spec.ts changed there), then open the PR to main.

## Next
- Rerun everything after the merge, open the PR, report to the lead.

## Known failing
- tests/test_orders_review.py::test_requirement_is_the_minimum_over_all_valid_pairings (known, fixed elsewhere).
