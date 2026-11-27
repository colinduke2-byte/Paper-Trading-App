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
