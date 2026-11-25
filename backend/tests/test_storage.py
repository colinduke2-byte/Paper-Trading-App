"""State files: loading every failure mode safely, migrations of real old files, segments, save failures."""
import errno
import glob
import json
import os
import shutil
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.core.clock import SimClock
from app.exchange import Exchange
from app.lifecycle.actions import split
from app.marketdata.feed import SimFeed
from app.storage import fsutil, schema
from app.storage.store import SaveBlocked, StateStore
from helpers import D, T0, make_exchange, move

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
RELEASED = sorted(glob.glob(os.path.join(FIXTURES, "states", "*", "state.json"))) + \
    [os.path.join(FIXTURES, "state_main", "state.json")]


def ledger_ok(account):
    assert schema.account_problems(account) == []


def trade(ex, feed, clock, a, n=3, symbol="ABC"):
    for i in range(n):
        ex.submit(a, {"symbol": symbol, "side": "buy" if i % 2 == 0 else "sell", "qty": 2 if i % 2 == 0 else 1})
        move(ex, feed, clock, symbol, "100.00")


def doc_of(ex) -> str:
    return json.dumps(ex.to_document(), sort_keys=True)


def reload(path, clock=None) -> Exchange:
    ex = Exchange(SimFeed(), clock or SimClock(T0), data_dir=str(path))
    ex.load()
    return ex


def fresh(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("Margin", "margin", D("100000")).id
    trade(ex, feed, clock, a)
    [w] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 2, "type": "limit", "limit_price": "50", "tif": "gtc"})
    ex.save(force=True)
    return ex, feed, clock, a


# ---- "no state yet" against "state present but unreadable" ------------------------------------------

def test_an_empty_folder_is_no_state_yet(tmp_path):
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(tmp_path))
    assert ex.load() is False and ex.load_result.status == "empty"
    assert ex.store.recovery is None and not ex.store.blocked
    ex.create_account("x", "cash", D(10))
    assert ex.save(force=True) and (tmp_path / "state.json").exists()


DAMAGE = {
    "truncated": lambda b: b[: len(b) // 2],
    "empty": lambda b: b"",
    "whitespace": lambda b: b"  \n",
    "invalid json": lambda b: b"{\"version\": 2, \"accounts\": [}" + b[40:],
    "not utf-8": lambda b: b"\xff\xfe" + b,
    "not an object": lambda b: b"[1, 2, 3]",
    "version is text": lambda b: json.dumps({**json.loads(b), "version": "two"}).encode(),
    "accounts wrong type": lambda b: json.dumps({**json.loads(b), "accounts": {"A1": 1}}).encode(),
    "number is text": lambda b: b.replace(b'"starting_cash":"100000"', b'"starting_cash":"lots"'),
    "missing seq": lambda b: json.dumps({k: v for k, v in json.loads(b).items() if k != "seq"}).encode(),
    "segment ref broken": lambda b: b.replace(b'"$segment":"segments/', b'"$segment":"../../etc/', 1),
}


@pytest.mark.parametrize("kind", sorted(DAMAGE))
def test_damaged_state_is_kept_and_never_overwritten(tmp_path, kind):
    ex, feed, clock, a = fresh(tmp_path)
    good = (tmp_path / "state.json").read_bytes()
    bad = DAMAGE[kind](good)
    assert bad != good
    (tmp_path / "state.json").write_bytes(bad)
    ex2 = Exchange(SimFeed(), SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load() is False
    rec = ex2.store.recovery
    assert ex2.load_result.status == "damaged" and rec["kind"] == "damaged" and "state.json" in rec["reason"]
    assert ex2.accounts == {}
    # the damaged file was copied aside with a time stamp, and stays in place
    copy = rec["copy"]
    assert os.path.basename(os.path.dirname(copy)) == "recovery" and open(os.path.join(copy, "state.json"), "rb").read() == bad
    assert "state.json" in open(os.path.join(copy, "reason.txt"), encoding="utf-8").read()
    # nothing overwrites it: not a save, not a forced save
    ex2.create_account("new", "cash", D(5))
    assert ex2.save(force=True) is False
    with pytest.raises(SaveBlocked):
        ex2.store.write(ex2.store.plan([], [], ex2.document_head()))
    assert (tmp_path / "state.json").read_bytes() == bad


def test_wrong_types_deep_inside_are_named(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    d = json.loads((tmp_path / "state.json").read_text())
    d["accounts"][0]["lots"]["ABC"][0][0] = [1]
    (tmp_path / "state.json").write_text(json.dumps(d))
    ex2 = reload(tmp_path)
    assert "accounts[0].lots.ABC[0][0] should be a number" in ex2.store.recovery["reason"]


def test_unbalanced_ledger_is_damage(tmp_path):
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(tmp_path))
    ex.create_account("x", "cash", D(10))
    ex.save(force=True)
    doc = ex.to_document()
    doc["accounts"][0]["ledger"][0]["postings"][0][1] = "11"
    (tmp_path / "state.json").write_text(json.dumps(doc))
    ex2 = reload(tmp_path)
    assert ex2.store.blocked and "unbalanced" in ex2.store.recovery["reason"]


def test_state_from_a_newer_version_is_refused_and_left_alone(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    d = json.loads((tmp_path / "state.json").read_text())
    d["version"] = schema.CURRENT_VERSION + 1
    d["something_new"] = {"x": 1}
    raw = json.dumps(d).encode()
    (tmp_path / "state.json").write_bytes(raw)
    ex2 = reload(tmp_path)
    assert ex2.load_result.status == "newer" and ex2.store.recovery["kind"] == "newer"
    assert "newer PaperDesk" in ex2.store.recovery["reason"]
    assert ex2.save(force=True) is False and (tmp_path / "state.json").read_bytes() == raw
    with pytest.raises(schema.NewerVersionError):
        schema.migrate(json.loads(raw))


def test_missing_or_short_segment_is_damage_and_a_cut_off_tail_is_not(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    seg = glob.glob(str(tmp_path / "segments" / "*.ledger.jsonl"))[0]
    raw = open(seg, "rb").read()
    # a cut-off line after the referenced entries (a crash during an append): ignored
    open(seg, "ab").write(b'{"id": 99, "ts": 1, "ki')
    ex2 = reload(tmp_path)
    assert not ex2.store.blocked and doc_of(ex2) == doc_of(ex)
    # fewer entries than state.json expects: damage
    open(seg, "wb").write(raw[: raw.rfind(b"\n", 0, len(raw) - 1) + 1])
    assert reload(tmp_path).store.blocked
    os.unlink(seg)
    ex3 = reload(tmp_path)
    assert ex3.store.blocked and "is missing" in ex3.store.recovery["reason"]


def test_damaged_market_file_alone_does_not_block(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    (tmp_path / "market.json").write_text("{not json")
    ex2 = reload(tmp_path)
    assert ex2.load_result.status == "ok" and not ex2.store.blocked
    assert any("market.json" in w for w in ex2.store.warnings)


def test_leftover_temp_files_are_removed_on_load(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    (tmp_path / ".state.json.abc.tmp").write_bytes(b"partial")
    (tmp_path / "notes.tmp").write_bytes(b"not ours")
    reload(tmp_path)
    assert not (tmp_path / ".state.json.abc.tmp").exists() and (tmp_path / "notes.tmp").exists()


# ---- migrations ---------------------------------------------------------------------------------------

def test_migrations_are_ordered_and_complete():
    assert sorted(schema.MIGRATIONS) == list(range(1, schema.CURRENT_VERSION))


@pytest.mark.parametrize("path", RELEASED, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_every_released_state_file_migrates_and_keeps_its_ledger(tmp_path, path):
    src = os.path.dirname(path)
    for name in ("state.json", "market.json"):
        if os.path.exists(os.path.join(src, name)):
            shutil.copy(os.path.join(src, name), tmp_path / name)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw.get("version", 1) <= schema.CURRENT_VERSION
    doc, start = schema.migrate(json.loads(json.dumps(raw)))
    schema.validate(doc)
    assert start == raw.get("version", 1)
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(tmp_path))
    assert ex.load() and ex.load_result.from_version == start
    assert len(ex.accounts) == len(raw["accounts"])
    for r in raw["accounts"]:
        acc = ex.account(r["id"])
        ledger_ok(acc)
        assert len(acc.ledger.transactions) == len(r["ledger"]) and len(acc.fills) == len(r["fills"])
        assert acc.cash == sum((D(v) for t in r["ledger"] for k, v in t["postings"] if k == "cash"), D(0))
    assert {o.id for o in ex.engine.orders.values()} == {o["id"] for o in raw["orders"]}
    # the old file is kept byte for byte before the first save converts it
    if start < schema.CURRENT_VERSION:
        [b] = [x for x in os.listdir(tmp_path / "backups") if x.startswith("paperdesk-pre-migration-")]
        import zipfile
        with zipfile.ZipFile(tmp_path / "backups" / b) as z:
            assert z.read("state.json") == open(path, "rb").read()
            assert json.loads(z.read("manifest.json"))["schema_version"] == start
    before = doc_of(ex)
    assert ex.save(force=True)
    assert json.loads((tmp_path / "state.json").read_text())["version"] == schema.CURRENT_VERSION
    ex2 = reload(tmp_path)
    assert ex2.load_result.from_version == schema.CURRENT_VERSION and doc_of(ex2) == before


def test_migration_fills_the_old_defaults_explicitly():
    raw = json.load(open(os.path.join(FIXTURES, "states", "v1-f194465-first-build", "state.json")))
    assert "lifecycle" not in raw and "journal" not in raw["accounts"][0]
    doc, _ = schema.migrate(raw)
    a = doc["accounts"][0]
    assert a["lot_method"] == "fifo" and a["journal"] == [] and isinstance(a["lifecycle"], dict)
    assert all(len(l) == 5 for ls in a["lots"].values() for l in ls)
    assert doc["lifecycle"] == {"last_date": None, "applied": [], "seq": 1, "splits": []}
    assert all("legs" in o and "good_till" in o for o in doc["orders"])


# ---- segments: saving in proportion to change -----------------------------------------------------------

def test_round_trip_through_segments(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    disk = json.loads((tmp_path / "state.json").read_text())
    assert disk["accounts"][0]["ledger"]["$segment"].startswith("segments/")
    assert any(isinstance(o, str) for o in disk["orders"]) and any(isinstance(o, dict) for o in disk["orders"])
    ex2 = reload(tmp_path, SimClock(clock.now()))
    assert doc_of(ex2) == doc_of(ex)
    assert list(ex2.engine.orders) == list(ex.engine.orders)  # engine order (fill priority) is kept


def test_a_save_after_one_trade_writes_little(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("Big", "margin", D("10000000")).id
    trade(ex, feed, clock, a, n=400)
    ex.save(force=True)
    full = len(json.dumps(ex.to_document()))
    trade(ex, feed, clock, a, n=1)
    assert ex.save()
    b = ex.store.last_bytes
    # The notification centre (alerts, on main) is rewritten whole; it is bounded and not part of this change.
    notices = len(json.dumps(ex.document_head()["notifications"]))
    assert b["appended"] < 3000 and b["state"] < (full - notices) / 10 + notices, (b, full, notices)
    assert doc_of(reload(tmp_path, SimClock(clock.now()))) == doc_of(ex)


def test_an_unchanged_state_is_not_rewritten(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    m = os.stat(tmp_path / "state.json").st_mtime_ns
    n = ex.store.saves
    os.utime(tmp_path / "state.json", ns=(1, 1))
    ex.save(force=True)
    assert os.stat(tmp_path / "state.json").st_mtime_ns == 1 and ex.store.saves == n + 1


def test_reset_and_delete_start_new_generations_and_old_files_go(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    b = ex.create_account("Other", "cash", D(1000)).id
    trade(ex, feed, clock, b, n=2)
    ex.save(force=True)
    before = set(os.listdir(tmp_path / "segments"))
    ex.reset_account(a)
    ex.delete_account(b)
    trade(ex, feed, clock, a, n=1)
    ex.save()
    after = set(os.listdir(tmp_path / "segments"))
    assert not any(n.startswith(f"{b}-") for n in after)
    assert not (before & after) - {n for n in before if n.startswith("orders-")}
    ex2 = reload(tmp_path, SimClock(clock.now()))
    assert doc_of(ex2) == doc_of(ex) and list(ex2.accounts) == [a]


def test_compaction_rewrites_everything_and_drops_the_old_files(tmp_path):
    ex, feed, clock, a = fresh(tmp_path)
    before = set(os.listdir(tmp_path / "segments"))
    ex.store.compact_next()
    ex.dirty = True
    ex.save()
    after = set(os.listdir(tmp_path / "segments"))
    assert before and after and not (before & after)
    assert doc_of(reload(tmp_path)) == doc_of(ex)


ACTIONS = st.lists(st.one_of(
    st.tuples(st.just("buy"), st.integers(1, 5)), st.tuples(st.just("sell"), st.integers(1, 5)),
    st.tuples(st.just("limit"), st.integers(1, 3)), st.tuples(st.just("cancel"), st.integers(0, 5)),
    st.tuples(st.just("price"), st.integers(90, 110)), st.tuples(st.just("save"), st.integers(0, 1)),
    st.tuples(st.just("reload"), st.just(0)), st.tuples(st.just("reset"), st.just(0)),
    st.tuples(st.just("account"), st.just(0)), st.tuples(st.just("compact"), st.just(0)),
    st.tuples(st.just("journal"), st.just(0))), min_size=1, max_size=30)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ACTIONS)
def test_property_what_was_saved_is_what_loads(tmp_path_factory, actions):
    """After any sequence of trading, resets, new accounts, compaction and reloads, loading the folder gives
    exactly the state at the last save, and every ledger invariant holds."""
    d = tmp_path_factory.mktemp("prop")
    ex, feed, clock = make_exchange(str(d))
    a = ex.create_account("P", "margin", D("1000000")).id
    saved = None
    price = 100
    for act, n in actions:
        if act in ("buy", "sell"):
            try:
                ex.submit(a, {"symbol": "ABC", "side": act, "qty": n})
            except ValueError:
                pass
        elif act == "limit":
            ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": n, "type": "limit", "limit_price": str(price - 5), "tif": "gtc"})
        elif act == "cancel":
            open_ = [o for o in ex.engine.active_orders() if o.account_id == a]
            if open_:
                ex.cancel(a, open_[n % len(open_)].id)
        elif act == "price":
            price = n
        elif act == "reset":
            ex.reset_account(a)
        elif act == "account":
            ex.create_account("More", "cash", D(500))
        elif act == "compact":
            ex.store.compact_next()
        elif act == "journal":
            ex.account(a).journal.append({"id": f"J{len(ex.account(a).journal) + 1}", "text": "note"})
            ex.dirty = True
        move(ex, feed, clock, "ABC", f"{price}.00")
        if act == "save":
            assert ex.save(force=bool(n))
            saved = doc_of(ex)
        elif act == "reload":
            ex.save(force=True)
            saved = doc_of(ex)
            ex2 = Exchange(feed, clock, data_dir=str(d))
            assert ex2.load()
            assert doc_of(ex2) == saved
            ex = ex2
            a = sorted(ex.accounts)[0]
    if saved is not None:
        ex3 = reload(d, SimClock(clock.now()))
        assert doc_of(ex3) == saved
        for acc in ex3.accounts.values():
            ledger_ok(acc)


# ---- save failures: disk full, read-only, file held open -------------------------------------------------

@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EROFS, errno.EACCES])
def test_failed_saves_are_reported_retried_and_keep_running(tmp_path, monkeypatch, err):
    now = [1000.0]
    ex, feed, clock, a = fresh(tmp_path)
    ex.store.wall = lambda: now[0]
    good = (tmp_path / "state.json").read_bytes()
    real = fsutil.atomic_write_bytes

    def broken(path, data):
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(fsutil, "atomic_write_bytes", broken)
    trade(ex, feed, clock, a, n=2)
    assert ex.save() is False
    s = ex.store.status()
    assert s["saving_failed"] and s["attempts"] == 1 and s["error"] and s["next_retry"] > now[0]
    assert {errno.ENOSPC: "disk is full", errno.EROFS: "read-only", errno.EACCES: "read-only"}[err] in s["error"]
    assert ex.dirty  # the change is still pending
    assert (tmp_path / "state.json").read_bytes() == good  # the old state is intact
    assert ex.save() is False and ex.store.attempts == 1  # waits for the back-off instead of hammering the disk
    now[0] += 3
    trade(ex, feed, clock, a, n=1)
    assert ex.save() is False and ex.store.attempts == 2
    monkeypatch.setattr(fsutil, "atomic_write_bytes", real)
    now[0] += 120
    assert ex.save() is True
    assert not ex.store.status()["saving_failed"] and not ex.dirty
    assert doc_of(reload(tmp_path, SimClock(clock.now()))) == doc_of(ex)


def test_a_failed_segment_append_leaves_no_garbage(tmp_path, monkeypatch):
    """A disk that fills up in the middle of appending to a segment: the partial bytes are cut off by the
    next append, so the file never holds a broken entry before good ones."""
    ex, feed, clock, a = fresh(tmp_path)
    import builtins
    real_open = builtins.open
    state = {"fail": True}

    class Half:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def truncate(self, n):
            self.f.truncate(n)

        def seek(self, n):
            self.f.seek(n)

        def write(self, data):
            self.f.write(data[: len(data) // 2])
            self.f.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self):
            pass

        def fileno(self):
            return self.f.fileno()

    def fake_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if state["fail"] and str(path).endswith(".jsonl") and mode in ("r+b", "wb"):
            return Half(f)
        return f
    monkeypatch.setattr(builtins, "open", fake_open)
    trade(ex, feed, clock, a, n=2)
    assert ex.save() is False
    state["fail"] = False
    ex.store.retry_at = 0
    assert ex.save() is True
    for seg in glob.glob(str(tmp_path / "segments" / "*.jsonl")):
        for line in open(seg, "rb").read().splitlines():
            json.loads(line)
    monkeypatch.undo()
    assert doc_of(reload(tmp_path, SimClock(clock.now()))) == doc_of(ex)


def test_save_plan_and_write_are_separate_steps(tmp_path):
    """The thread-safety model: serialise under the exchange lock, write outside it. Plans written out of
    order never go backwards."""
    ex, feed, clock, a = fresh(tmp_path)
    trade(ex, feed, clock, a, n=1)
    p1 = ex.save_plan()
    trade(ex, feed, clock, a, n=1)
    p2 = ex.save_plan()
    assert ex.write_plan(p2) and ex.write_plan(p1)  # p1 is older: skipped
    assert doc_of(reload(tmp_path, SimClock(clock.now()))) == doc_of(ex)


# ---- state.json and market.json that disagree ------------------------------------------------------------

EX_DATE = __import__("datetime").date(2026, 11, 25)


def test_split_in_state_but_not_in_market_is_reapplied_to_the_feed(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("S", "margin", D("100000")).id
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    feed.schedule(split("ABC", EX_DATE, 2))
    clock.advance(86400)
    ex.tick()
    assert ex.account(a).position("ABC") == 20
    ex.save(force=True)
    feed2 = SimFeed()
    feed2.set_quote("ABC", D("199.99"), D("200.00"))  # a market from before the split (market.json lost)
    feed2.schedule(split("ABC", EX_DATE, 2))
    ex2 = Exchange(feed2, SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    assert ex2.account(a).position("ABC") == 20 and feed2.quote("ABC").last == D("100.00")  # prices follow the accounts
    clock2 = ex2.clock
    clock2.advance(86400)
    ex2.tick()
    assert ex2.account(a).position("ABC") == 20  # not split twice


def test_split_in_market_but_not_in_state_is_applied_to_accounts_once(tmp_path):
    """state.json older than market.json (a restored state, say): the lifecycle catches up from its own
    last date and books the split for the accounts; the feed, already adjusted, is not adjusted again."""
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("S", "margin", D("100000")).id
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 10})
    ex.save(force=True)
    old_state = (tmp_path / "state.json").read_bytes()
    segs = {n: (tmp_path / "segments" / n).read_bytes() for n in os.listdir(tmp_path / "segments")}
    feed.schedule(split("ABC", EX_DATE, 2))
    clock.advance(86400)
    ex.tick()
    assert feed.applied_actions
    (tmp_path / "state.json").write_bytes(old_state)
    for n, b in segs.items():
        (tmp_path / "segments" / n).write_bytes(b)
    feed.quotes["ABC"].last  # the feed keeps its adjusted market
    ex2 = Exchange(feed, SimClock(clock.now()), data_dir=str(tmp_path))
    assert ex2.load()
    assert ex2.account(a).position("ABC") == 10
    ex2.tick()
    assert ex2.account(a).position("ABC") == 20 and feed.quote("ABC").last == D("50.00")
    ledger_ok(ex2.account(a))
