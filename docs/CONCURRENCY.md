# Concurrency

PaperDesk runs one process. Inside it, several threads and one event loop use the same `Exchange`
(accounts, ledger, matching engine, lifecycle, feed). This page says who runs where, the one rule that
keeps them apart, and what a new route or background thread has to do.

## The model in one paragraph

There is **one lock per server, `ctx.lock`** (`app/core/locking.py`, a re-entrant mutex). Whoever reads or
changes exchange state holds it for the whole operation: the tick, every API route, mode switches
between live and replay, and taking the snapshot that is saved to disk. It covers both exchanges
(`ctx.live` and the replay sandbox) and the `ctx.ex` / `ctx.replay` pointers. Nothing slow happens
while it is held: no network, no disk write, no WebSocket send, no sleep. Waiting for it is always
bounded: a request that cannot get it within 2 seconds answers `503` with
`{"detail": "The simulator is busy. Try again in a moment.", "busy": true}` and `Retry-After: 1`; the
tick asks with priority and, if a request really is stuck, skips the turn and names the holder in the log.

One coarse lock was chosen over finer ones (per account, readers/writer) because every interesting
operation crosses objects: a fill touches the engine, an account, its ledger, the lifecycle state and
the event queue; a tick touches all accounts; a reset swaps an account under its orders. Operations
are short (a submit is about 0.4 ms, a tick 0.4 to 8 ms), so one lock costs almost nothing and
there is no lock order between exchange objects to get wrong.

## Who runs where

| Context | Thread | What it does with exchange state | How it is made safe |
|---|---|---|---|
| Tick loop (`server.py: loop`, `turn`) | asyncio task that hands each turn to the `paperdesk-tick` thread | `ex.tick()` or `replay.advance()`, `ex.save_plan()`, builds one message per WebSocket client | `turn()` holds `ctx.lock` (priority waiter). Returns the snapshot text and the messages |
| Saving (`ex.write_plan`) | `paperdesk-save` thread | none: writes text produced under the lock | Runs without `ctx.lock`. `Exchange._io_lock` serialises writers; snapshots are numbered, so an older one never replaces a newer file |
| Broadcast (`server.py: broadcast`) | event loop | none: sends the messages `turn()` built | No lock held while awaiting `send_json` |
| WebSocket endpoint `/ws` | event loop | none: edits its own entry in `ctx.clients` | `ctx.clients` is only replaced item by item; readers copy it with `list(...)` |
| Plain `def` routes under `/api` (orders, accounts, settings, journal, watchlists, alerts and notifications, reports, lifecycle, replay pause/resume/speed, status) | Starlette worker threads, up to 40 at once | read and change everything | Wrapped centrally by `app/api/locking.py`: the whole handler runs under `ctx.lock`, and its result is copied before the lock is released |
| Market data routes (`/api/symbols`, `quotes`, `bars`, `depth`, `trades`, `fundamentals`, `movers`, `screener`, `options/*`) | worker threads | read the feed only (a live feed may call the provider) | `@unlocked`. The feed hands data over through its own structures (below) |
| `/api/accounts/{id}/analytics`, `/statements/{month}` (+`/html`) | worker threads | price history from the feed, then the account | `@unlocked`: fetch prices first, then one `with ctx.locked():` block for the account |
| `/api/research/*` | worker threads | provider data; `ctx.ex.feed` and `is_stock` only | exempt by path |
| `/api/chartstate*` | worker threads | none: chart settings in `chartstate.json` | exempt by path. Its own lock guards its document and its file write (a leaf lock) |
| `/api/auth/*`, access middleware | event loop (login is `async`) and worker threads | none: the session store | exempt by path. `Sessions.lock` and the login limiter are leaf locks |
| Replay start / exit (`ctx.enter_replay`, `ctx.exit_replay`) | worker threads | swap `ctx.ex`, `ctx.replay`; snapshot live or the sandbox | `@unlocked` routes; the swap and snapshot happen in one `ctx.locked()` block, the session file is read before it and state files are written after it |
| Replay step / seek (`ReplayController._command`) | worker threads | run up to thousands of sandbox ticks | `@unlocked` routes. The controller holds its command lock for the whole command and takes `ctx.lock` in slices of about 20 ms, so ticks, broadcasts and other requests carry on |
| Live feed: stream thread (`alpaca-stream`) and poller (`alpaca-poller`) | daemon threads | must not touch exchange state. They write quotes into the feed's own map under `AlpacaFeed._lock`; `feed.step()` (called by the tick, under `ctx.lock`) moves them into the exchange | Hand-over through the feed's lock. The poller asks which symbols are wanted through `ctx.live_interest()`, which reads under `ctx.lock` with a 0.5 s limit and otherwise returns the previous list |
| Alert price history (`alpaca-history`, `AlpacaFeed.load_wanted_bars`) | daemon thread | must not touch exchange state. It fetches the bars that alerts asked for and stores them in the feed's bar cache under `AlpacaFeed._lock` | Alerts are created and evaluated under `ctx.lock`, so they read history only with `feed.bars_ready()`, which answers from memory or returns `None` and notes the request. The alert is not evaluated until a later tick finds the history in memory (see "Alerts and price history") |
| Provider corporate actions (in progress on another branch) | background fetch | same rule as the feed threads | Fetch in the background into a structure with its own lock; `feed.corporate_actions()` is called by the tick under `ctx.lock`, so it must only return what is already in memory |
| Start-up and shut-down (`default_exchange`, `lifespan`) | main thread / event loop | load, final save | Load happens before any thread exists. Shut-down stops the tick and save threads, then snapshots under the lock and writes after releasing it |

## Lock order

1. `ReplayController._lock` (the replay command lock) may be held when `ctx.lock` is taken. Never the
   other way round with a blocking wait: code that holds `ctx.lock` only *tries* the command lock
   (`advance()` skips its turn; `step()`/`seek()` called under the lock refuse with "another step or
   seek is still running").
2. `ctx.lock`, then `AlpacaFeed._lock` (inside `feed.step()`), then nothing.
3. `ctx.lock` may be held when `Exchange._io_lock` is taken (a direct `ex.save()` in tests and tools).
   The server never does this: it writes after releasing `ctx.lock`. A thread holding `_io_lock`
   never asks for `ctx.lock`.
4. `Sessions.lock`, the login limiter, `RateLimiter.lock`, the research cache lock, the chart-state
   lock and the replay route's `starting` mutex are leaves: nothing else is taken while they are held, and the auth code
   never touches `ctx.lock`.

## What the lock guarantees

- **One exchange per request.** `ctx.ex` cannot change while a locked route runs, so a request works
  entirely on live or entirely on the sandbox. Entering or leaving a replay waits for requests in flight.
- **Live state is untouched by a replay.** The live snapshot is taken in the same locked block that
  switches to the sandbox, so no in-flight request can change live after it was saved.
- **One order per `client_id`.** The duplicate check and the insert are one locked step.
- **Cancel against fill.** Each runs whole; a cancel either wins (the order is canceled, no later fill)
  or answers "order is no longer open".
- **Reset or delete during working orders or forced liquidation.** A tick (including all liquidation
  steps of that tick) and a reset never interleave. Orders of a deleted account are removed with it.
- **Atomic order changes.** `Exchange.submit`, `cancel`, `cancel_all` and `replace` run inside
  `Exchange._atomic`: if anything raises half way (the second leg of a multi-leg fill, a lifecycle
  hook, an invalid field of a replace), the account's ledger, lots, fills, lifecycle state, its orders,
  the event queue and the fill counter are put back. Order ids used by a failed request are not reused.
- **Notifications.** The notifier listens on `ex.events` (an `EventStream`); every append happens under
  the lock, so notifications are written under it too, and a rolled-back order change takes its
  notifications back.
- **Saving in two steps.** `plan = ex.save_plan()` serialises under the lock and does no I/O;
  `ex.write_plan(plan)` does only I/O and is called after the lock is released.
- **State files.** `state.json` is serialised to text under the lock and written with an atomic
  replace, so every file on disk is a consistent state that loads. `load()` parses the whole file
  before it changes anything.
- **The tick is never starved.** It waits in a priority lane; ordinary waiters are served first come,
  first served. `ctx.metrics` counts turns, skipped turns, tick errors and the longest wait.

Not covered:

- A fault *inside a tick* (a bug) is reported and the market goes on, but the part of the tick that
  already ran is kept; the tick is not transactional.
- Taking the snapshot for `state.json` serialises the whole state under the lock (about 8 ms per
  500 orders with their fills). It is the longest regular hold; an incremental format would shorten it.

## Alerts and price history

Indicator alerts and volume alerts measured against the average need bars. `Alerting.create` runs in a
locked route and `Alerting.on_tick` runs in the tick, so neither may call `feed.bars()` (with a live
feed that is a provider request of up to several seconds). They use two feed methods instead:

- `feed.bars_ready(symbol, tf, limit, after=None)` returns bars from memory, or `None` when they are not
  there. It never makes a request. Feeds that keep their bars in memory (synthetic, replay, `SimFeed`)
  always answer, so nothing changes for them.
- `feed.history_mark()` returns a mark for "now". Passed as `after`, only history read after the mark
  counts. The engine takes a mark when a bar closes, so the closed bar is never read from an older load.

On `None` the alert waits: an indicator alert shows "waiting for price history" and its series is seeded
on a later tick; a closed bar is pushed on the first tick after its load; an average-volume alert shows
"no volume history yet". `AlpacaFeed` notes each request and its `alpaca-history` thread loads it
(`load_wanted_bars`, also called directly by tests), retrying a failed load after 10 seconds. If loads
lag by four bars or more the series is rebuilt from history, as after a clock jump.

Calendar alerts read `feed.corporate_actions()`, which must also answer from memory. When it raises
(a live feed that is still loading its actions) the alert shows "calendar unavailable: ..." and is tried
again every 5 seconds of exchange time instead of waiting for the next day.

`tests/test_concurrency.py::test_alerts_never_ask_the_data_provider_while_holding_the_lock` creates
indicator, volume and calendar alerts on a live feed and ticks under the lock; it fails on any provider
request made by the route or the tick.

## Adding a route

Do nothing special: write a plain `def` route in a `routes_<feature>.py` module, use `ctx.ex`, return
plain data (dicts, lists, numbers; `ctx.plain(...)` for dataclasses and Decimals). It runs under the
lock, sees one consistent exchange and its changes apply as a unit. `tests/test_concurrency.py` walks
`app.routes` and fails if a route under `/api` is not locked and not on its short list of exceptions.

Rules:

- **Never do anything slow in a locked route**: no provider or other network call (`feed.bars`,
  `feed.option_chain`, `feed.option_expirations`, the research hub), no file reading or writing,
  no `time.sleep`. A test runs every GET route against a live feed and fails if a provider request is
  made while the lock is held.
- If the route needs something slow, mark it and lock only around the state:

  ```python
  from .locking import unlocked

  @app.get("/api/accounts/{aid}/something")
  @unlocked("fetches bars from the feed before reading the account")
  def something(aid: str):
      ex = ctx.ex                       # look the exchange up once and keep using this one
      bars = ex.feed.bars("SPY", "1d")  # slow part, no lock
      with ctx.locked():                # raises Busy (503) if it cannot get the lock
          a = ex.account(aid)
          return ctx.plain(build(a, bars))   # build the whole answer inside the block
  ```

  Then add the path to the list in `tests/test_concurrency.py` with the reason.
- No `async def` routes under `/api` unless marked `@unlocked` (registration fails otherwise): the
  event loop must never wait for a thread lock. An async route that needs exchange state does the
  work in a worker thread (`await run_in_threadpool(fn)`) with `ctx.locked()` inside `fn`.
- Do not keep references to exchange objects between requests, and do not return live containers from
  an unlocked route (copy them inside the locked block).

## Adding a background thread

- It never calls into the exchange, its accounts or its engine directly, not even to read.
- Data flows in through a structure the thread owns, protected by its own lock or a queue, and is
  picked up by the tick (as `feed.step()` does) under `ctx.lock`.
- If it must run something on exchange state, it does so in `with ctx.locked(): ...` (or
  `ctx.lock.acquire(timeout)`), briefly, with all I/O outside the block, and it treats `Busy` as
  "try again later".
- No Unix-only primitives (`fork`, signals, `fcntl`): only `threading`, so Windows behaves the same.

## Settings

- `PAPERDESK_LOCK_WAIT` (seconds, default 2.0): how long a request waits for the lock before `503`.
- `TICK_WAIT` in `server.py` (1.0 s): how long the tick waits before it skips a turn.

## Tests

- `tests/test_concurrency.py`: the lock, route coverage, `503` while the lock is stuck, no provider
  call under the lock (routes, alert creation and alert evaluation in the tick), tick priority, atomic order changes, ordered saves, sliced replay commands.
- `tests/test_concurrency_stress.py`: many threads submit, cancel, replace, reset, change settings,
  step a replay and switch modes while the loop ticks every 5 ms; then the ledger invariants, order
  and fill accounting and every `state.json` written along the way are checked. Seeded and bounded
  (about 15 s). `PAPERDESK_STRESS=1` (or a larger number) runs a long soak of the same tests.
