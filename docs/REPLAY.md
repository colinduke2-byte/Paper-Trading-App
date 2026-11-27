# Historical replay

Replay plays back one recorded trading session with the clock under your control: play, pause,
step, speed from 1x to 60x, and jump forward to a time. You trade in a sandbox account; your normal
paper accounts are saved before the replay starts and are left exactly as they were.

## Using it

Press **Replay** in the top bar, pick a session, choose the starting cash and account type, and
start. While the replay runs:

- The banner reads "Replay of <date>" with the data source, so replayed prices are never mistaken
  for the current market.
- The control bar shows the simulated date and New York time (and your local time, which is what
  the chart axis uses), play/pause, +1 min and +15 min steps, speed, a progress slider and a
  "Jump to HH:MM" box. The slider and jump only go forward: a replay cannot go back in time, because
  your orders and fills up to now cannot be undone. Exit and start the session again to see an
  earlier part.
- Quotes, charts, depth, time and sales, option chains, order entry, positions and every other
  panel work as usual, on the replayed market.
- **Exit replay** returns to your paper accounts. The sandbox is discarded.

The bundled sessions are **simulated**: they come from the synthetic feed (fictional companies)
with fixed seeds, and are labelled "Simulated data" everywhere. Sessions you record from a real
data provider are labelled "Recorded data".

| Bundled session | Notes |
|---|---|
| `sample-2025-11-28` | Day after Thanksgiving, early close at 1:00 pm |
| `sample-2026-03-13` | Friday; weekly options expire at the close |
| `sample-2026-06-17` | Ordinary Wednesday |

Each has 7 symbols (MKTX, TECX, NVLX, ORBT, AUTO, FNBK, ENRG), a snapshot every 5 seconds over the
regular session, and 260 earlier daily bars for chart context. Together they are about 1 MB.

## How the clock works

A replay has its own `SimClock`, `ReplayFeed`, `Exchange` (session rules `us`) and data directory
(`<PAPERDESK_DATA>/replay`, emptied at each start). The server's `ctx.ex` points at the replay
exchange while it runs, so every existing endpoint serves the replay without change.

The clock moves in whole simulated seconds and the exchange is ticked once for every second, so
order fills, day-order expiry at the close (1:00 pm on early-close days), option expiry and
exercise at the close, alerts and equity samples all happen at the simulated time. While playing,
the server's tick loop runs every 0.25 s, adds `speed x elapsed wall time` to a carry, and runs that
many whole seconds; at 60x that is 15 exchange ticks per loop. Work per loop is capped at 240 ticks:
if the machine cannot keep up, the replay falls behind (status `lagging`) instead of freezing. Step
and seek run the same per-second ticks synchronously (a whole 6.5-hour session takes about 0.3 s).

Because the feed releases events only by the time the exchange passes in, the same session file and
the same commands at the same simulated times always give a byte-identical ledger
(`tests/test_replay.py::test_deterministic_playback_gives_identical_ledgers`). The synthesised
depth ladder uses a stable hash for the same reason.

While a replay runs, the live exchange is not ticked: live orders do not work and the live
simulated market does not move until you exit. A server restart during a replay comes back in live
mode; the replay is not resumed.

## API

All under `/api/replay`; errors use the normal `{"detail": ...}` body.

| Method and path | Body | Result |
|---|---|---|
| `GET /sessions` | | List of sessions: id, label, date, start, end, symbols, simulated, source, size, bundled (or `error` for an unreadable file) |
| `GET /status` | | `{"active": false}` or the replay status below |
| `POST /start` | `session`, `starting_cash`, `account_type` (`margin`/`cash`), optional `speed`, `play` | Status. 409 if a replay is already running, 404 for an unknown session, 422 for a damaged file |
| `POST /pause`, `POST /resume` | | Status. 409 when no replay runs |
| `POST /speed` | `speed` 1 to 60 | Status |
| `POST /step` | `seconds` 1 to 3600 (default 60) | Status |
| `POST /seek` | `time` (epoch seconds) or `offset` (seconds from the session start) | Status. 400 for a time in the past |
| `POST /exit` | | `{"active": false}` |

Replay status: `active, session_id, label, date, description, source, simulated, time, start, end,
progress (0..1), speed, paused, ended, lagging, jumps, session, run_id`. `/api/status` also carries
`mode` (`live` or `replay`) and `replay` (this object or null); its `time` is the simulated time.
The sandbox account ids start at `A9001` so they never look like live ids.

## Session file format (version 1)

A session file is gzip-compressed UTF-8 text, one JSON value per line, named `<id>.pdsession.gz`.
It can be written and read as a stream. The gzip header has no file name or time stamp, so the same
content always gives the same bytes.

```
{"format":"paperdesk-session","version":1, ...header...}
{"daily":"NVLX","bars":[[ts,open,high,low,close,volume], ...]}      context, optional, any number
{"minutes":"NVLX","bars":[[ts,open,high,low,close,volume], ...]}    context, optional
["Q",ts,"NVLX",bid,ask,bid_size,ask_size]                           quote update
["T",ts,"NVLX",price,size]                                          trade print
...
{"end":{"events":N,"end":ts}}                                       end marker, required
```

**Header fields.** `format` and `version` (required; readers refuse a higher version than they
know), `date` (New York trading date, ISO), `start` (epoch seconds), `end` (planned end or null),
`label`, `description`, `timezone` (`America/New_York`), `source` (where the data came from),
`simulated` (true for synthetic data), `symbols` (list of objects with at least `symbol`; also
`name`, `sector`, `type`, `vol` (annual volatility used by the option model), `shares_outstanding`,
`eps`, `dividend`), and `initial` (per symbol: `bid, ask, bid_size, ask_size, last, open, high, low,
volume, prev_close` as decimal strings: the state at `start`).

**Events.** Times are epoch seconds (integers or decimals) and never decrease. Prices and sizes are
JSON numbers written from exact decimals without exponent, and read back as `Decimal`, so they round
trip exactly. A `Q` followed by a `T` for the same symbol at the same time is played as one update
(the print on the new book). Volume, high and low are derived from the prints; `open` and
`prev_close` come from `initial`.

**End marker.** Holds the event count and the actual end time. A file without it is reported as
incomplete (for example a recording that was cut off). Unknown object lines are ignored, so a later
1.x writer can add context without breaking older readers.

## Making sessions

From `backend/`:

```bash
# Simulate a whole trading day with the synthetic feed (labelled simulated)
python -m app.marketdata.replay record --date 2026-03-13 --seed 11 --symbols NVLX,ORBT --out mine
# Record a running provider live, in real time, for 10 minutes
python -m app.marketdata.replay record --realtime 600 --feed package.module:ProviderClass --out live
# Build a session from 1-minute bars
python -m app.marketdata.replay build --bars minutes.csv --daily daily.csv --meta meta.json --out day
# Build a session for a real past day from Alpaca's historical 1-minute bars (needs Alpaca keys;
# see docs/DATA_PROVIDERS.md, "Practising on real past days")
python -m app.marketdata.replay fetch --date 2026-03-13 --symbols SPY,AAPL --out real-day
# Summarise a file
python -m app.marketdata.replay inspect mine.pdsession.gz
# Regenerate the bundled samples (byte-identical output)
python -m app.marketdata.replay samples
```

Put your own files in `<PAPERDESK_DATA>/sessions/` and they appear in the session list.

In code, `SessionRecorder(provider, path, ...)` records any `MarketDataProvider`: `start(now)`
writes the header from the provider's current quotes, symbol info and daily bars, `capture(quotes)`
takes what `provider.step()` returned, and `finish(end)` writes the end marker. `record(provider,
path, times)` drives a provider through a list of times; `realtime(seconds, interval)` yields wall
clock times for recording a live provider as it runs.

### Building from 1-minute bars: the approximation

Bars say where the price went in each minute, not how. The builder turns each bar into four prints
at :00, :15, :30 and :45 seconds: the open, then the extreme on the open's side (the low first on
an up bar, the high first on a down bar), then the other extreme, then the close. The bar's volume
is split evenly over the four prints (whole shares, remainder on the close). A bar with no volume
gives one quote update and no prints. The quote around each print is one spread wide (`--spread-bps`,
default 2 basis points, at least one cent): a print at or above the previous print trades at the
ask, a lower one at the bid. Displayed size on each side is a twentieth of the bar's volume, rounded
down to a round lot, at least 100 shares. Nothing is random.

So fills on a built session are approximate inside each minute: a limit order between the bar's
high and low fills, but the exact second and the queue are invented. Real tick data recorded with
the recorder does not have this limitation. Bar CSV columns: `symbol,time,open,high,low,close,volume`;
`time` is epoch seconds or ISO 8601 (without an offset it is New York time). Annual volatility for
the option model is taken from `meta.json` or estimated from the daily (else the minute) closes.
