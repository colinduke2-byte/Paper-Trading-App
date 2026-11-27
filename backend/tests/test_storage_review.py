"""Regression tests for the independent review of PR 19 (one per finding, built from the reviewer's repros)."""
import errno
import json
import os
import shutil

import pytest

from app.storage import archive, fsutil, schema
from app.storage.lock import DataDirLock
from test_storage_faults import Crash, busy_exchange, check_loads, digest
from helpers import D, move


# ---- 1. an order id reused after a crash must not load the lost order -------------------------------

def test_order_id_reused_after_crash_loads_the_lost_order(tmp_path, monkeypatch):
    ex, feed, clock, a = busy_exchange(tmp_path)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 3})
    move(ex, feed, clock, "ABC", "100.00")
    real = fsutil.replace

    def die_on_state(s, d):
        if d.endswith("state.json"):
            raise Crash()
        return real(s, d)
    monkeypatch.setattr(fsutil, "replace", die_on_state)
    with pytest.raises(Crash):
        ex.save(force=True)  # the finished buy was appended to the orders segment, state.json never renamed
    monkeypatch.setattr(fsutil, "replace", real)
    ex2 = check_loads(tmp_path)
    ex2.feed, ex2.clock = feed, clock
    ex2.submit(a, {"symbol": "ABC", "side": "sell", "qty": 1})  # gets the id the lost buy had
    move(ex2, feed, clock, "ABC", "100.00")
    oid = list(ex2.engine.orders)[-1]
    mine = ex2.engine.orders[oid]
    ex2.save(force=True)
    got = check_loads(tmp_path).engine.orders[oid]
    assert (got.side, got.qty) == (mine.side, mine.qty)
    # and the next reload after more trading still holds the right one
    ex3 = check_loads(tmp_path)
    assert digest(ex3) == digest(ex2)


def test_crash_reload_trade_save_reload_at_every_crash_point(tmp_path, monkeypatch):
    """The fault harness step the review asked for: after each crash point, reload, trade, save, reload."""
    k = 0
    while True:
        d = tmp_path / f"run{k}"
        ex, feed, clock, a = busy_exchange(d)
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 3})
        move(ex, feed, clock, "ABC", "100.00")
        calls = {"n": 0}

        def maybe_die(real):
            def f(*args, **kw):
                calls["n"] += 1
                if calls["n"] == k + 1:
                    raise Crash()
                return real(*args, **kw)
            return f
        with monkeypatch.context() as m:
            m.setattr(os, "fsync", maybe_die(os.fsync))
            m.setattr(os, "replace", maybe_die(os.replace))
            try:
                ex.save(force=True)
                finished = True
            except Crash:
                finished = False
        ex2 = check_loads(d)
        ex2.feed, ex2.clock = feed, clock
        for side, qty in (("sell", 1), ("buy", 2), ("sell", 1)):
            ex2.submit(a, {"symbol": "ABC", "side": side, "qty": qty})
            move(ex2, feed, clock, "ABC", "100.00")
        assert ex2.save(force=True)
        assert digest(check_loads(d)) == digest(ex2), f"crash at operation {k + 1}"
        if finished:
            break
        k += 1


# ---- 2. a market-only plan must not make an older state plan be skipped --------------------------------

def test_market_only_plan_does_not_hide_a_pending_state_plan(tmp_path):
    ex, feed, clock, a = busy_exchange(tmp_path)
    feed.snapshot = lambda: {"quotes": {}}  # a feed that has a market to save
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(ex, feed, clock, "ABC", "100.00")
    p1 = ex.save_plan()  # state due
    assert p1.state is not None
    ex._last_market_save = float("-inf")
    p2 = ex.save_plan()  # market only
    assert p2.state is None and p2.market is not None
    assert ex.write_plan(p2) and ex.write_plan(p1)
    assert digest(check_loads(tmp_path)) == digest(ex)
    # and the reverse: a newer state-only plan does not drop an older plan's market
    ex._last_market_save = float("-inf")
    feed.snapshot = lambda: {"quotes": {"marker": 1}}
    p3 = ex.save_plan(force=True)
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(ex, feed, clock, "ABC", "100.00")
    ex._last_market_save = clock.now()
    p4 = ex.save_plan()
    assert p4.market is None and p3.market is not None
    assert ex.write_plan(p4) and ex.write_plan(p3)
    assert json.loads((tmp_path / "market.json").read_text()) == {"quotes": {"marker": 1}}
    assert digest(check_loads(tmp_path)) == digest(ex)


# ---- 3. when the recovery copy fails, a recovery choice must not destroy the damaged files ----------------

def _no_space(*a, **k):
    raise OSError(errno.ENOSPC, "No space left on device")


def test_failed_recovery_copy_is_retried_then_files_are_moved_aside(tmp_path, monkeypatch):
    ex, feed, clock, a = busy_exchange(tmp_path)
    raw = (tmp_path / "state.json").read_bytes()
    bad = raw[: len(raw) // 2]
    (tmp_path / "state.json").write_bytes(bad)
    segs = {n: (tmp_path / "segments" / n).read_bytes() for n in os.listdir(tmp_path / "segments")}
    monkeypatch.setattr(shutil, "copy2", _no_space)
    monkeypatch.setattr(shutil, "copytree", _no_space)
    ex2 = check_loads_damaged(tmp_path)
    st = ex2.store
    assert st.recovery["copy"] is None
    # what "start fresh" does: ensure a copy first, then save
    where = st.ensure_copy()
    assert "damaged-" in where
    st.end_recovery()
    ex2.create_account("Fresh", "cash", D(100))
    assert ex2.save(force=True)
    # the damaged bytes and the old segments are still on disk
    aside = [n for n in os.listdir(tmp_path) if n.startswith("state.json.damaged-")]
    assert len(aside) == 1 and (tmp_path / aside[0]).read_bytes() == bad
    seg_aside = [n for n in os.listdir(tmp_path) if n.startswith("segments.damaged-")][0]
    assert {n: (tmp_path / seg_aside / n).read_bytes() for n in os.listdir(tmp_path / seg_aside)} == segs


def test_recovery_choice_is_refused_when_no_copy_can_be_kept(tmp_path, monkeypatch):
    ex, feed, clock, a = busy_exchange(tmp_path)
    (tmp_path / "state.json").write_bytes(b"{")
    monkeypatch.setattr(shutil, "copy2", _no_space)
    monkeypatch.setattr(shutil, "copytree", _no_space)
    ex2 = check_loads_damaged(tmp_path)
    monkeypatch.setattr(os, "rename", _no_space)
    with pytest.raises(RuntimeError, match="nothing was changed"):
        ex2.store.ensure_copy()
    assert ex2.store.blocked and (tmp_path / "state.json").read_bytes() == b"{"


def test_recovery_api_refuses_start_fresh_without_a_copy(tmp_path, monkeypatch):
    from test_data_api import SAME, build
    ex, feed, clock, a = busy_exchange(tmp_path / "data")
    (tmp_path / "data" / "state.json").write_bytes(b"{")
    monkeypatch.setattr(shutil, "copy2", _no_space)
    monkeypatch.setattr(shutil, "copytree", _no_space)
    monkeypatch.setattr(os, "rename", _no_space)
    c, ex2, app = build(tmp_path, monkeypatch)
    r = c.post("/api/data/recovery/fresh", headers=SAME)
    assert r.status_code == 409 and "could not be copied or moved aside" in r.json()["detail"]
    assert (tmp_path / "data" / "state.json").read_bytes() == b"{"


def check_loads_damaged(path):
    from app.core.clock import SimClock
    from app.exchange import Exchange
    from app.marketdata.feed import SimFeed
    from helpers import T0
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(path))
    assert ex.load() is False and ex.store.blocked
    return ex


# ---- 4. counters at or below an existing id are raised ------------------------------------------------

def test_restored_counters_never_reuse_existing_ids(tmp_path):
    ex, feed, clock, a = busy_exchange(tmp_path)
    doc = ex.to_document()
    first = doc["orders"][0]["id"]
    doc["seq"].update(order=1, fill=1, account=1, alert=1, expiry=1)
    doc["lifecycle"]["seq"] = 1
    checked, _, _ = archive.check_document(json.loads(json.dumps(doc)))
    assert checked["seq"]["order"] > max(int(o["id"][1:]) for o in doc["orders"])
    assert checked["seq"]["fill"] > max(int(f["id"][1:]) for x in doc["accounts"] for f in x["fills"])
    assert checked["seq"]["account"] > max(int(x["id"][1:]) for x in doc["accounts"])
    ex.apply_document(checked)
    before = ex.engine.orders[first]
    [o] = ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(ex, feed, clock, "ABC", "100.00")
    assert o.id != first and ex.engine.orders[first] is before
    assert len({f.id for x in ex.accounts.values() for f in x.fills}) == sum(len(x.fills) for x in ex.accounts.values())
    acct = ex.create_account("New", "cash", D(10))
    assert acct.id not in {x["id"] for x in doc["accounts"]}


# ---- 5. a restore that fails part-way changes nothing -------------------------------------------------

def test_failed_restore_leaves_the_state_as_it_was(tmp_path, monkeypatch):
    from test_data_api import SAME, ZIP, build, trade
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    name = c.post("/api/data/backups", headers=SAME).json()["name"]
    trade(c, "A0001", 2)
    before = json.dumps(ex.to_document(), sort_keys=True)
    svc = app.state.data
    arc = svc.backups.read(name)
    arc.sessions = {"2026-11-20-mine.pdsession.gz": b"x"}

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(svc, "_write_sessions", full)
    with pytest.raises(OSError):
        svc.restore(arc)
    assert json.dumps(ex.to_document(), sort_keys=True) == before
    # a failure inside the state itself puts the old state back as well
    monkeypatch.undo()
    arc = svc.backups.read(name)
    real = ex.lifecycle.restore

    def broken(d):
        if d is arc.doc["lifecycle"] or (isinstance(d, dict) and d.get("_boom")):
            raise ValueError("boom")
        return real(d)
    arc.doc["lifecycle"]["_boom"] = True
    monkeypatch.setattr(ex.lifecycle, "restore", broken)
    with pytest.raises(ValueError):
        svc.restore(arc)
    assert json.dumps(ex.to_document(), sort_keys=True) == before


@pytest.mark.parametrize("name", ["sessions/CON", "sessions/con.pdsession.gz", "sessions/nul", "sessions/COM1.x",
                                  "sessions/lpt9", "sessions/aux.txt", "sessions/x.", "STATE.JSON"])
def test_windows_device_names_are_refused(name):
    assert not archive._NAME.match(name)
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("state.json", "{}")
        z.writestr(name, "x")
    with pytest.raises(archive.ArchiveError, match="cannot place safely"):
        archive.read(buf.getvalue())


# ---- 6. JSON members are capped well below the session-file cap ---------------------------------------

def test_state_json_is_capped_at_64_mb_unpacked():
    import io
    import zipfile
    assert archive.MAX_JSON == 64 * 1024 * 1024 < archive.MAX_MEMBER
    big = b'{"version": 2, "pad": "' + b"a1" * (33 * 1024 * 1024) + b'"}'  # 66 MB, compresses about 1000:1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", "{}")
        z.writestr("state.json", big)
    with pytest.raises(archive.ArchiveError, match="more than PaperDesk accepts|zip bomb"):
        archive.read(buf.getvalue())
    old = archive.MAX_RATIO
    try:  # with the ratio check out of the way, the size cap alone refuses it
        archive.MAX_RATIO = 10 ** 9
        with pytest.raises(archive.ArchiveError, match="more than PaperDesk accepts"):
            archive.read(buf.getvalue())
    finally:
        archive.MAX_RATIO = old


# ---- 7. the fallback lock keeps its own heartbeat ------------------------------------------------------

def test_fallback_lock_refreshes_its_own_heartbeat(tmp_path, monkeypatch):
    from app.storage import lock as lockmod
    monkeypatch.setattr(lockmod, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(DataDirLock, "_os_lock", lambda self, fh: (_ for _ in ()).throw(OSError(errno.ENOLCK, "x")))
    now = [1000.0]
    held = DataDirLock(str(tmp_path), wall=lambda: now[0]).acquire()
    try:
        assert held.fallback
        now[0] += 900  # longer than STALE_SECONDS: only a heartbeat keeps the folder held
        import time
        deadline = time.time() + 5
        while time.time() < deadline:
            info = json.loads((tmp_path / ".paperdesk.lock.json").read_text())
            if info["heartbeat"] == now[0]:
                break
            time.sleep(0.02)
        assert info["heartbeat"] == now[0]
        with pytest.raises(lockmod.DataDirLocked):
            DataDirLock(str(tmp_path), wall=lambda: now[0]).acquire()
    finally:
        held.release()


# ---- 8. an in-place edit of a saved entry is caught -----------------------------------------------------

def test_in_place_edit_of_the_last_saved_entry_is_not_lost(tmp_path):
    ex, feed, clock, a = busy_exchange(tmp_path)
    acct = ex.account(a)
    acct.closed[-1] = dict(acct.closed[-1], note="edited in place")  # same length, last entry changed
    acct.fills[-1].kind = "trade-edited"
    ex.dirty = True
    assert ex.save()
    assert digest(check_loads(tmp_path)) == digest(ex)


def test_list_cleared_and_refilled_to_the_same_length_is_not_lost(tmp_path):
    ex, feed, clock, a = busy_exchange(tmp_path)
    acct = ex.account(a)
    n = len(acct.closed)
    acct.closed[:] = [dict(c, pnl="0") for c in acct.closed]
    assert len(acct.closed) == n
    ex.dirty = True
    assert ex.save()
    assert digest(check_loads(tmp_path)) == digest(ex)
