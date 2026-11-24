# Saved state: format, safety and backups

Everything PaperDesk remembers lives in one data folder (`PAPERDESK_DATA`, default `backend/data`). This page
describes what is in it, how it is kept safe, and the rules for changing the format. Code: `backend/app/storage/`.

## The data folder

```
data/
  .paperdesk.lock         held by the running server (one server per folder)
  .paperdesk.lock.json    who holds it: process id, host, start time (only used for the error message)
  state.json              accounts, orders, watchlists, alerts, counters, lifecycle (format below)
  segments/               append-only parts of state.json (ledger, fills, closed trades, finished orders)
  market.json             the simulated feed's market (prices and history); rebuilt if lost
  sessions.json           sign-ins (LAN mode); never in backups or exports
  sessions/               replay sessions you recorded
  backups/                automatic and manual backups (zip archives)
  recovery/<time>/        copies of files that could not be read, with reason.txt
  replay/                 the replay sandbox (wiped at every replay start; never backed up)
  alpaca/                 the same layout again for the Alpaca data mode (real symbols never mix with fictional ones)
```

Each data mode (the simulated feed in `data/`, Alpaca in `data/alpaca/`) has its own `state.json`, segments,
backups and recovery copies. Backups and exports cover the mode the server runs in.

## The state document (format version 2)

The *logical* state is one JSON object. Money and quantities are decimal strings, times are epoch seconds.

| Key | Content |
|---|---|
| `version` | format version, an integer. Missing means 1 |
| `app_version` | the PaperDesk version that wrote it (information only) |
| `accounts` | list of accounts: `id`, `name`, `type` (`cash`/`margin`), `starting_cash`, `created_at`, `ledger` (transactions: `id`, `ts`, `kind`, `memo`, `ref`, `postings` [[account, amount]]), `lots` ({symbol: [[qty, price, ts, cost, opening fill id]]}), `fills`, `closed`, `equity_history`, `day_start_equity`, `lot_method`, `journal`, `settings`, `lifecycle` |
| `orders` | every order the engine knows, in engine order (that order is the fill priority, so it is kept) |
| `watchlists`, `alerts` | as shown in the UI |
| `seq` | next number of each id counter: `order`, `account`, `alert`, `fill`, `expiry` |
| `lifecycle` | `last_date`, `applied` corporate action ids, `seq`, `splits` (the splits the accounts reflect) |

On disk, `state.json` keeps the big lists that only ever grow in `segments/` instead of inline:

* an account's `ledger`, `fills` and `closed` may be `{"$segment": "segments/<account>-<token>.<list>.jsonl", "count": N}`:
  the list is the first N lines of that file, one JSON value per line;
* an entry of `orders` may be a string, the id of a finished order whose JSON is a line in the file named by
  `orders_segment` (`{"$segment": "segments/orders-<token>.orders.jsonl"}`).

Backups, archives and account exports always hold the self-contained form (everything inline); a reference to a
segment in an uploaded file is refused.

## Saving

A save appends the new entries to the segment files (written and fsync'd first) and then replaces `state.json`
atomically (temporary file in the same folder, fsync, rename, fsync of the folder on Linux and macOS). The counts in
`state.json` only ever refer to entries already on disk, so a crash or `kill -9` at any point leaves the previous
`state.json`, which still loads: at most the last save interval (one tick, normally one second) is lost. A cut-off
line at the end of a segment (a crash in the middle of an append) is ignored, and the next append cuts it off
before writing. Segment files that the current `state.json` no longer names (after a reset, a restore, an
import, or compaction at each automatic backup) are deleted after the next good save. An unchanged `state.json`
is not rewritten.

The save is two steps so that it fits a server that serialises under its exchange lock and writes outside it:
`plan = ex.save_plan()` (pure: builds bytes, no file I/O) and `ex.write_plan(plan)` (file I/O only). Plans written
out of order never go backwards (an older plan than the last one written is skipped).

`tests/test_storage_faults.py` checks this by dying at every fsync, rename and delete of a save, and by killing
a writer process with SIGKILL at random moments; after each, the folder must load, pass the ledger checks, and
hold the state of a save that was started and no older than the last save that finished.

### Measured cost

`tools/measure_save.py` (one margin account, one stock, alternating buys and sells; Linux, SSD):

| fills | full rewrite (before) | a save after one more trade (now) |
|---|---|---|
| 1,000 | 1.1 MB, 15 ms to serialise + 3 ms to write | 48 KB state.json + 0.8 KB appended, 1 ms + 3 ms |
| 5,000 | 5.7 MB, 83 ms + 9 ms | 234 KB + 0.8 KB, 4 ms + 3 ms |
| 10,000 | 11.4 MB, 222 ms + 15 ms | 466 KB + 0.8 KB, 9 ms + 3 ms |

Before this change every save rewrote everything, and the tick loop saves whenever anything changed (every
second while orders work), so 10,000 fills meant about 11 MB written and a quarter of a second of serialising per
second. What still grows with history: `state.json` lists one id per finished order (about 12 bytes each) and
the bounded equity history (6,000 points). At 100,000 orders `state.json` would be about 1.5 MB; a separate
index for finished orders is the next step if that ever matters.

## Loading, and what happens when it fails

| Found | Result |
|---|---|
| no `state.json` | "no state yet": the server starts with two new paper accounts |
| a valid file, any released version | loaded; older versions are converted (below) |
| empty, cut-off or invalid JSON, not UTF-8, wrong types, a missing or short segment, an unbalanced ledger | **recovery**: "damaged" |
| a `version` above what this build reads | **recovery**: "newer" (never converted downwards, never overwritten) |
| `market.json` unreadable | not fatal: the price history starts again, a warning shows in Data and backups |

In recovery the files are copied to `recovery/<time>/` (with `reason.txt`), the reason is logged, **saving is
switched off** (nothing can overwrite the files), and the app shows a recovery screen instead of the trading
screen, with three choices:

* **Restore the latest backup** (or pick another one): the newest backup that passes every check;
* **Start fresh** with new paper accounts (the damaged files stay in `recovery/`);
* **Quit** without changing anything (stops the server, as Ctrl+C would).

The same is available over the API: `GET /api/data/recovery`, `POST /api/data/recovery/restore {"backup"?}`,
`POST /api/data/recovery/fresh`, `POST /api/data/recovery/quit`; `/api/status` carries `storage.recovery`.

`state.json` and `market.json` that disagree are reconciled from what exists: the lifecycle records the splits
the accounts reflect and re-applies to the feed any that `market.json` lacks; a `state.json` older than
`market.json` catches up day by day and books a split for the accounts once, without adjusting the feed again.

### When saving fails

A full disk, a read-only folder or a file held open by another program: the server keeps running in memory,
`/api/status` shows `storage.saving_failed` and the error, the app shows a notice under the header, and the save
is retried with a back-off (2, 4, 8 … up to 60 seconds). When it works again the notice goes away.

### One server per folder

The server takes an operating-system lock on `.paperdesk.lock` (`flock` on Linux and macOS, `msvcrt.locking` on
Windows) and holds it while it runs. A second server on the same folder stops at start-up with "Another PaperDesk
server (process N on HOST, started …) is already using the data folder …". The operating system drops the lock
when the process ends, however it ends, so a lock file left by a crash never blocks a restart. On file systems
that cannot lock files the info file decides: held while that host's process is alive (Linux, macOS) or while its
heartbeat (refreshed every 30 seconds by the running server) is younger than 10 minutes.

## Versions and migrations: the rules for changing the format

1. A change that an older build could misread bumps `CURRENT_VERSION` in `backend/app/storage/schema.py` and adds
   a function to `MIGRATIONS` that turns version N-1 into N. Migrations run in order, are pure functions of the
   document, and are never edited once released: fix forward with a new version.
2. A new *optional* field with a harmless default does not need a new version: add the default to
   `fill_defaults()` (run on every load) and to `validate()` if it has a type to check.
3. Every released format gets a fixture: run `tools/make_state_fixture.py <backend dir of that build> <out dir>`
   (it drives the build's own API: stock, short, option and cash-account trades, a working order, a watchlist and
   an alert) and add the result under `backend/tests/fixtures/states/v<N>-<commit>-<name>/`.
   `tests/test_storage.py::test_every_released_state_file_migrates_and_keeps_its_ledger` migrates each one, loads
   it, and checks the ledger invariants, balances and counts.
4. Before the first save after a conversion, the original files are kept byte for byte as a `pre-migration`
   backup.
5. Unknown newer versions are refused, never "best-effort" loaded.

Version history:

| Version | Written by | Change |
|---|---|---|
| 1 | every build up to the first release of this page (fixtures from f194465, 1411b26, b27209a, 736d5ef, 1a84ad2) | fields were added over time and read with defaults; `version` was always 1 |
| 2 | this build | every field present (the old defaults moved into the 1→2 migration); segments; `app_version` |

## Backups

**Automatic** backups go to `backups/` every `PAPERDESK_BACKUP_INTERVAL` seconds (default 3600; 0 turns them off),
also after a start when the last one is older than that. Pruning keeps the newest backup of each of the last
`PAPERDESK_BACKUP_KEEP_HOURLY` hours (24) and `PAPERDESK_BACKUP_KEEP_DAILY` days (14), never the newest one, only
files whose names it wrote, and only after a new backup passed its check. No automatic backup is taken while a
replay runs, during a mode switch (the same lock), or in recovery.

**Snapshots before destructive actions**: resetting or deleting an account, restoring, importing an account and
converting an old format each take a backup first (`pre-reset`, `pre-delete`, `pre-restore`, `pre-import`,
`pre-migration`; the newest `PAPERDESK_BACKUP_KEEP_EVENTS`, default 20, are kept). If that backup fails the action
is refused.

Every backup is written atomically and then **verified by reading it back** in full (checksums, conversion,
structure, ledger checks); one that fails is deleted and the error shown in Data and backups.

## Archives (download and restore)

`GET /api/data/export` downloads a zip of everything in the current data mode: `manifest.json` (format
`paperdesk-backup`, format version, state format version, app version, created time, feed, and a SHA-256 and size
for every file), `state.json` (self-contained), `market.json` and recorded replay sessions. It never contains
`sessions.json` (sign-ins), `.env`, API keys or the lock files. Automatic backups have the same format without
the replay sessions.

Restoring (`POST /api/data/restore/preview` with the zip as the body, or `{"backup": name}`; then
`POST /api/data/restore/apply {"token"}`) checks the whole file before anything is touched: at most 64 MB
uploaded, at most 200 files, only the names above (no absolute paths, `..`, backslashes, folders or links), no
encryption, at most 256 MB per file and 512 MB in total once unpacked, no file compressed more than 100:1 above
1 MB (decompression bombs), the real unpacked size against the declared one, every checksum, then the state's
version, conversion, structure and ledger invariants. The preview says what would change, account by account,
and which symbols are not in the current feed. Applying takes a `pre-restore` backup first.

## Moving one account

`GET /api/data/accounts/{id}/export` downloads one account with its orders (format `paperdesk-account`, with a
checksum). `POST /api/data/accounts/import/preview` (the file as the body) and `.../import/apply {"token", "name"}`
add it to another installation or data mode as a new account: the account id, every order id and every fill id
get new numbers from the target's counters (references in the ledger, lots, closed trades and lifecycle follow).
Symbols that do not exist in the target feed are listed; their positions stay at cost and cannot be priced or
traded there, and their open orders are canceled on import.

All of these routes are under `/api/`, so the access middleware applies (Origin check on every write, a session
when a passcode is set). The UI is the **Data** button in the top bar (in the menu on a phone).
