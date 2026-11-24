# Progress: stream/backup (durable, portable user data)

PR: https://github.com/colinduke2-byte/Paper-Trading-App/pull/19 (base main, not merged)

## Done
- Recovery on damaged or newer state (copy to recovery/, saving off, recovery screen: restore / fresh / quit)
- Visible save failures with retry and back-off; one server per data folder (OS lock, stale-lock rule)
- Schema v2 with ordered migrations, v1 fixtures from old commits, forward refusal
- Segment (append-only) saves: 11.4 MB to about 0.47 MB per save at 10k fills; fault-injection and kill -9 tests
- Rotating verified backups and pre-action snapshots; zip archive download and restore with a dry run; account export and import
- Data and backups UI (desktop and phone); docs/STATE_FORMAT.md
- Tests: backend 480 passed, frontend vitest 38 passed, e2e 88 passed / 3 skipped (2 on purpose: my phone tests run on Pixel 7 only; 1 is chart.auth.spec from main)

## In progress
- Nothing. Waiting for review.

## Next (descoped)
- An index for finished orders, so state.json stops growing with order history
- Restore one account from a full backup

## Known failing tests
- None on this branch
