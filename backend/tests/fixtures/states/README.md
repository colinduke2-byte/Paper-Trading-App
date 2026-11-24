State files written by released builds, for the migration tests (docs/STATE_FORMAT.md, "Versions and migrations").

Each was produced on 2026-11-24 by checking the commit out in a temporary git worktree and running
`tools/make_state_fixture.py <that worktree>/backend <out dir>`, which drives that build's own API with the
synthetic feed: a margin account (stock buy, short sale, partial sale, a working GTC limit order, an option buy),
a cash account (buy and partial sale), a watchlist and an alert.

  v1-f194465-first-build   f194465  first backend build (also has its market.json)
  v1-1411b26-packaging     1411b26  main after PR 1 (packaging)
  v1-736d5ef-main-access   736d5ef  main after PR 7 (access mode)
  v1-1a84ad2-wave1         1a84ad2  integration/wave1 before the backup work (lifecycle, orders, reports fields)

../state_main/state.json is the older fixture from main at b27209a (see test_integration_wave1.py).
