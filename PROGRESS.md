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

# Cross-browser stream: progress

Branch `stream/crossbrowser`, pull request #11 (now targeting `main`). Updated after every meaningful step.

## Done
- `e2e/playwright.config.ts`: projects per engine, chosen with `E2E_ENGINES` (local default: chromium only).
  Firefox: desktop, sign-in. WebKit: desktop Safari, iPhone 14 portrait, iPhone 13 landscape, iPad portrait
  (phone layout) and landscape (desktop layout), sign-in. Chromium adds Pixel 7 landscape. Retries off.
  `E2E_BASE_URL` (test an already running server), `E2E_CHANNEL=msedge`.
- CI (`.github/workflows/ci.yml`): one e2e job per engine (WebKit in 2 shards); artifacts only on failure;
  new Windows job runs desktop + sign-in suites in Edge against a server started by `run.bat`, time zone set
  to Pacific. A step prints new/changed visual baselines into the job log (artifact downloads are blocked from
  the agent container); `tools/baselines_from_log.py` unpacks them.
- `a11y.spec.ts` (axe-core, serious/critical fail), `keyboard.spec.ts` (keyboard trading path, dialog focus
  trap/Escape/restore), `visual.spec.ts` + `visual.css` + `screens.ts` (baselines for desktop, desktop-firefox,
  desktop-safari, iphone-13, pixel-7, iphone-14-webkit committed, made on the CI runner).
- Home screen: `frontend/public/manifest.webmanifest`, icons (original SVG mark, `tools/make_icons.mjs`),
  apple-touch-icon, favicon, iOS meta tags; `PUBLIC_STATIC` exact allowlist in `backend/app/api/auth.py`,
  served by the UI catch-all in `server.py`; tests in `backend/tests/test_access.py`. README section.
- Docs: `e2e/README.md`, README "Tests and CI" and home-screen sections.

## Product bugs found
- FIXED WebKit, iPad landscape / 900-1100 px desktop layout: chart autoSize and page layout resized each other
  in a loop (ResizeObserver error, tabs never "stable"); after the first fix the chart spilled 37 px over the
  bottom tabs (also in Chromium). `styles.css` narrow-layout rules; test "narrow desktop window" in app.spec.ts.
- FIXED phone shell: Reports links 32-35 px tall at widths 700-900 px (Pixel 7 landscape, iPad portrait).
  `mobile.css`.
- FIXED a11y: charts were role img containing the library's attribution link (now role figure, tests updated);
  account dialog had no name; quote grid and time-and-sales scroll regions not keyboard reachable; no focus
  ring on links / tabindex elements; no dialog had focus management (new `frontend/src/dialogs.ts`).
- NOT A BUG, handled in the test fixture: WebKit logs "Viewport argument key interactive-widget not recognized"
  at error level (index.html sets it on purpose for Android). Only that exact message is ignored.
- OPEN, a11y: watchlist rows cannot be picked by keyboard (search + Enter works).
- OPEN, unexplained: once (round 1) a Firefox sign-in test's first page load hung 60 s; it did not recur in
  round 3. Watch for it.

## CI results per browser (latest finished round, commit 26d416d)
- chromium: all pass except visual (no baselines yet then) and Pixel 7 rotation test (fixed since).
- firefox: all pass except visual (no baselines then).
- webkit shard 1 (desktop Safari, iPhone 14): all pass except visual (no baselines then).
- webkit shard 2 (iPad, iPhone landscape, sign-in): 4 iPad-landscape failures, the chart overlap (fixed since).
- windows Edge via run.bat: all pass.
Round 4 (a0e650a, baselines + fixes) and round 5 (merge of main) not read yet.

## In progress / next
- Read CI for the merge of main (new chart tools, alerts, help dialog specs now also run in Firefox/WebKit;
  triage any engine-specific failures; the Help dialog has its own Escape handling next to dialogs.ts).
- New screens from main (Help, alerts bell, chart tools) may need visual baselines updated: use the
  `update-visual-baselines` label flow in e2e/README.md.
- Final report to the lead.

## Known failing tests
- backend: tests/test_orders_review.py::test_requirement_is_the_minimum_over_all_valid_pairings (fixed elsewhere).
