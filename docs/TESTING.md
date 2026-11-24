# Testing PaperDesk

All commands run from `backend/` with the shared virtualenv
(`/home/user/paperdesk/backend/.venv/bin/python`, written `python` below).

## The normal suite

    python -m pytest -q

Runs the unit, property (Hypothesis) and integration tests, plus a fast seeded soak run
(`tests/soak/`, about 25 s). Tests marked `xfail(strict=True)` describe confirmed bugs that are not
fixed yet: each names the file and the cause in its reason. When a fix makes one pass, the suite
fails on purpose ("XPASS(strict)"): remove the marker, and remove the invariant's name from
`tools/soak/invariants.py` `CONFIRMED` if it is listed there.

## Soak simulator (`tools/soak/`)

A deterministic long-run simulator that drives the real `Exchange` with the synthetic feed and a
simulated clock. A seeded random trader runs three to five accounts (cash and margin, every lot
method, varied settings) through every feature: all order types including on-open and on-close,
good-till-date, one-cancels-other, one-triggers-other, brackets, every multi-leg template
(verticals, straddles, strangles, condors, butterflies, calendars, diagonals, covered calls,
collars, ratio spreads...), replaces and cancels, short selling, option buying and writing held
through expiry, account resets and deletes, settings and lot-method changes, save/load cycles,
offline gaps (save or "crash", jump the clock days or weeks, start again from the files), replay
sessions entered and left in the middle, price shocks (gaps that cause margin calls, liquidations
and in-the-money expiries) and the feed's corporate actions plus extra seeded ones (reverse and
odd-ratio splits, special dividends).

After every step it checks the cheap invariants (new ledger transactions balance, position
accounts equal the lots' cost basis, cash accounts hold no short stock and no negative cash, no
expired option is held, every order that changed is consistent). Every `--full-every` steps it
checks everything: the ledger replays to its balances, the cash identity derived from the ledger
accounts, fills against orders, one-cancels-other and bracket groups never over-fill, summary
equity equals cash plus marked positions plus pending dividends, no NaN, Infinity or float money in
state, and (every `--api-every` full checks) every read and export route of every account through
the HTTP API with cross-checks (gains report total = ledger realised, current statement closes on
the summary, statements reconcile, CSV and positions agree). Every reload checks that save is
current and load-then-save is byte-identical, and that summaries and positions survive it.

    python -m tools.soak --seed 7 --days 365                  one seed
    python -m tools.soak --seeds 0-299 --days 120 --jobs 3     a campaign, with totals
    python -m tools.soak --seed 7 --days 365 --minimize        delta-debug a failing run to a short command list
    python -m tools.soak --seed 7 --trace failing.json         replay a saved command list (--trace-dir writes them)
    python -m tools.soak --determinism --seed 7 --days 60      byte-compare runs in processes with different hash seeds
    python -m tools.soak --known ''                            stop on confirmed failures too (default: count them)

Long version inside pytest: `PAPERDESK_SOAK=1 PAPERDESK_SOAK_SEEDS=0-19 PAPERDESK_SOAK_DAYS=365
python -m pytest -q tests/soak/test_soak.py`.

A failure prints the seed, step, invariant name, message and the command; `--minimize` adds the
smallest command list it found (within `--budget` seconds) that still breaks the same invariant.
Confirmed failures (listed in `invariants.CONFIRMED`, each with a reproduction in
`tests/soak/test_findings.py`) are counted and reported at the end instead of stopping the run,
so a campaign can find new ones.

## HTTP fuzzing (`tools/soak/fuzz.py`)

`tests/soak/test_fuzz_api.py` sends schema-free random and adversarial JSON (wrong types, huge
numbers, NaN and Infinity as text, deep nesting, odd unicode, missing and extra fields, and valid
bodies with one field mutated) to every non-GET `/api` route, then checks the invariants and a
save/load round trip. `PAPERDESK_FUZZ_REQUESTS=5000 python -m pytest -q tests/soak/test_fuzz_api.py`
for a longer run. Server errors already reported are listed in `KNOWN` there.

## Disk and long uptime (`tests/soak/test_disk.py`)

Damaged, truncated and incomplete state files at start-up, a full or read-only data directory
during save and in the tick loop.

## Determinism (`tests/soak/test_determinism.py`)

The same seed and commands must give byte-identical state in one process (`test_soak.py`) and
across processes with different `PYTHONHASHSEED` values.

## Benchmarks (`tools/bench.py`, not in the suite)

    python -m tools.bench                      every scenario, then a markdown table
    python -m tools.bench --only orders,ledger
    python -m tools.bench --profile options    cProfile of one scenario's slow case (orders, options, ledger, symbols)

Scenarios: tick latency and CPU with 26 / 500 / 5000 symbols; with 10 to 5000 working orders and
with thousands of finished orders kept; with 1 to 200 held option positions; with 1 to 20
accounts; ledgers of 1e3 to 1e5 transactions (save time and size, load time, route latency); the
cost of `json.dump` against `json.dumps` for the saved files; the WebSocket tick payload. The
server loop ticks once a second, so a tick (plus its save) above 1 s means the app falls behind.
