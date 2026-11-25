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
  `orders_segment` (`{"$segment": "segments/orders-<token>.orders.jsonl"}`). The file can also hold lines that
  `state.json` does not name: orders removed by a reset, or orders appended by a save that was cut off before its
  `state.json` was written. After such a crash the order counter is back where `state.json` left it, so an id
  in a stale line can be given out again. Two rules keep that safe: only the ids `state.json` names count as
  saved (a reused id is appended again when its new order finishes), and when an id appears on more than one
  line the last one wins (a saved order's line is never followed by another line for the same id).

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
out of order never go backwards. The state part and the market part are tracked separately: the state part of a
plan is skipped only if a newer plan's state part was written already, and likewise for the market part. A
market-only plan never causes a state plan to be skipped, or the other way round.

The lists in segments are meant to be append-only. Each save checks the last entry already on disk for every
list against memory. If it was changed in place, or a list was cleared and refilled to the same length, that
account's segments are rewritten in full (and a warning is logged) instead of keeping the old version. A
change to an older entry that leaves the last one alone is not detected until the next compaction, so code that
edits history must replace the list object (that starts a new generation) rather than edit it.

`tests/test_storage_faults.py` checks this by dying at every fsync, rename and delete of a save, and by killing
a writer process with SIGKILL at random moments; after each, the folder must load, pass the ledger checks, and
hold the state of a save that was started and no older than the last save that finished.

### Measured cost

`tools/measure_save.py` (one margin account, one stock, alternating buys and sells; Linux, SSD):

| fills | full rewrite (before) | a save after one more trade (now) |
|---|---|---|
| 1,000 | 1.3 MB, 14 ms to serialise + 3 ms to write | 166 KB state.json + 0.8 KB appended, 1 ms + 2 ms |
| 5,000 | 5.8 MB, 66 ms + 8 ms | 352 KB + 0.8 KB, 4 ms + 2 ms |
| 10,000 | 11.5 MB, 140 ms + 15 ms | 584 KB + 0.8 KB, 7 ms + 3 ms |

(Measured again after merging the notification centre from main: its list, up to about 120 KB, is in every
state.json.)

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

Before either of the first two choices overwrites anything, the damaged files must be kept somewhere. The copy to
`recovery/` is made at start-up. If it failed (usually because the disk is full, which is also a common cause of
the damage), the copy is tried again. Failing that, `state.json` and `segments/` are renamed aside, to
`state.json.damaged-<time>` and `segments.damaged-<time>`, which needs no space. If even that fails, the choice is
refused and nothing changes. The recovery screen says when no copy exists yet.
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
heartbeat (refreshed every minute by a thread of the lock itself) is younger than 10 minutes. Two limits of
that fallback:
* the age rule is checked first, so a holder that stops refreshing for 10 minutes (a suspended laptop) loses
  the folder even though its process is alive;
* the check-then-write is not atomic, so two servers started within the same moment on such a file system could
  both start.

The OS lock has neither problem. Only the live exchange holds the lock: a replay runs a sandbox exchange in
`replay/`, and the live exchange, which keeps the lock, stays the same.

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
6. A new kind of id needs its counter added to `schema.raise_counters()`.

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
encryption, at most 64 MB unpacked for `manifest.json`, `state.json` and `market.json` (they are parsed in memory),
256 MB for each replay session and 512 MB in total, no session file name that Windows reserves for a device
(`CON`, `NUL`, `COM1` …) or that ends in a dot, no file compressed more than 100:1 above
1 MB (decompression bombs), the real unpacked size against the declared one, every checksum, then the state's
version, conversion, structure and ledger invariants. The preview says what would change, account by account,
and which symbols are not in the current feed. Applying takes a `pre-restore` backup first, then writes the
replay session files, then replaces the state in memory. If any step fails, the state stays as it was.

Every id counter in a loaded, restored or imported document is raised above the largest id of its kind already
in it: orders `O…`, fills `F…`, lifecycle fills `L…` (the lifecycle's own counter), expiry fills `X…`,
accounts `A…` and alerts `AL…`. So a hand-edited or inconsistent backup can never make the next order or fill
replace an existing one.

## Moving one account

`GET /api/data/accounts/{id}/export` downloads one account with its orders (format `paperdesk-account`, with a
checksum). `POST /api/data/accounts/import/preview` (the file as the body) and `.../import/apply {"token", "name"}`
add it to another installation or data mode as a new account: the account id, every order id and every fill id
get new numbers from the target's counters (references in the ledger, lots, closed trades and lifecycle follow).
Symbols that do not exist in the target feed are listed; their positions stay at cost and cannot be priced or
traded there, and their open orders are canceled on import.

All of these routes are under `/api/`, so the access middleware applies (Origin check on every write, a session
when a passcode is set). The UI is the **Data** button in the top bar (in the menu on a phone).
