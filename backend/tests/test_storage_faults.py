"""Fault injection: a writer killed at any point leaves a loadable, consistent state at most one save old;
and only one server can use a data folder."""
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.storage import fsutil, schema
from app.storage.lock import DataDirLock, DataDirLocked
from helpers import D, T0, make_exchange, move

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def digest(ex) -> str:
    return hashlib.sha256(json.dumps(ex.to_document(), sort_keys=True).encode()).hexdigest()


def check_loads(path) -> Exchange:
    ex = Exchange(SimFeed(), SimClock(T0), data_dir=str(path))
    ok = ex.load()
    assert ex.load_result.status in ("ok", "empty"), ex.load_result.reason
    assert ok == (ex.load_result.status == "ok")
    for a in ex.accounts.values():
        assert schema.account_problems(a) == []
    return ex


class Crash(BaseException):
    """Stands for the process dying: nothing after it runs (not even except-handlers for Exception)."""


def busy_exchange(tmp_path):
    ex, feed, clock = make_exchange(str(tmp_path))
    a = ex.create_account("F", "margin", D("1000000")).id
    for i in range(6):
        ex.submit(a, {"symbol": "ABC", "side": "buy" if i % 2 == 0 else "sell", "qty": 2 if i % 2 == 0 else 1})
        move(ex, feed, clock, "ABC", "100.00")
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1, "type": "limit", "limit_price": "90", "tif": "gtc"})
    ex.save(force=True)
    return ex, feed, clock, a


def test_crash_at_every_file_operation_of_a_save(tmp_path, monkeypatch):
    """Systematic: run one save, dying at the k-th fsync / rename / delete, for every k. After each crash the
    folder must load, and hold either the state before that save or the state after it."""
    k = 0
    while True:
        d = tmp_path / f"run{k}"
        ex, feed, clock, a = busy_exchange(d)
        before = digest(ex)
        # changes that touch every kind of file: new fills, a finished order, a reset (new generation and
        # deletion of old segment files), a new account
        ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 3})
        move(ex, feed, clock, "ABC", "100.00")
        b = ex.create_account("G", "cash", D(500)).id
        ex.submit(b, {"symbol": "ABC", "side": "buy", "qty": 1})
        move(ex, feed, clock, "ABC", "100.00")
        ex.store.compact_next()
        after = digest(ex)
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
            m.setattr(os, "unlink", maybe_die(os.unlink))
            try:
                ex.save(force=True)
                finished = True
            except Crash:
                finished = False
        loaded = check_loads(d)
        assert digest(loaded) in (before, after), f"crash at operation {k + 1}"
        if finished:
            assert digest(loaded) == after
            break
        k += 1
    assert k >= 6  # the save really had that many crash points


WRITER = textwrap.dedent("""
    import json, hashlib, os, random, sys
    sys.path.insert(0, sys.argv[3]); sys.path.insert(0, os.path.join(sys.argv[3], "tests"))
    from helpers import D, make_exchange, move
    data, log = sys.argv[1], sys.argv[2]
    rnd = random.Random(int(sys.argv[4]))
    ex, feed, clock = make_exchange(data)
    accts = [ex.create_account("K", "margin", D("10000000")).id]
    out = open(log, "a")
    def note(kind, ex):
        h = hashlib.sha256(json.dumps(ex.to_document(), sort_keys=True).encode()).hexdigest()
        out.write(kind + " " + h + "\\n"); out.flush(); os.fsync(out.fileno())
    i = 0
    while True:
        i += 1
        a = rnd.choice(accts)
        r = rnd.random()
        if r < 0.02:
            ex.reset_account(a)
        elif r < 0.04:
            accts.append(ex.create_account("K", "margin", D("100000")).id)
        elif r < 0.06:
            ex.store.compact_next()
        else:
            try:
                ex.submit(a, {"symbol": "ABC", "side": rnd.choice(["buy", "sell"]), "qty": rnd.randint(1, 5)})
            except ValueError:
                pass
        move(ex, feed, clock, "ABC", "%d.00" % rnd.randint(95, 105))
        note("intent", ex)
        ok = ex.save(force=rnd.random() < 0.3)
        if ok:
            note("done", ex)
""")


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL")
def test_kill_minus_nine_at_random_points(tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(WRITER)
    rnd = random.Random(1234)
    for run in range(12):
        d, log = tmp_path / f"data{run}", tmp_path / f"log{run}.txt"
        p = subprocess.Popen([sys.executable, str(script), str(d), str(log), BACKEND, str(run)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # let it build up some history, then kill it at a random moment
        deadline = time.time() + 20
        while time.time() < deadline and (not log.exists() or log.stat().st_size < 3000):
            time.sleep(0.02)
        time.sleep(rnd.uniform(0.0, 0.4))
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        lines = [ln.split() for ln in log.read_text().splitlines() if len(ln.split()) == 2]
        assert lines, p.stderr.read().decode()[-2000:]
        intents = [h for k, h in lines if k == "intent"]
        dones = [h for k, h in lines if k == "done"]
        loaded = digest(check_loads(d))
        # it is one of the states a save was started for, and no older than the last finished save
        assert loaded in intents, f"run {run}: loaded state was never saved"
        if dones:
            last_done = len(intents) - 1 - intents[::-1].index(dones[-1])
            pos = len(intents) - 1 - intents[::-1].index(loaded)
            assert pos >= last_done, f"run {run}: loaded an older state than the last finished save"


# ---- one server per data folder ----------------------------------------------------------------------

def test_second_lock_on_the_same_folder_is_refused(tmp_path):
    a = DataDirLock(str(tmp_path)).acquire()
    with pytest.raises(DataDirLocked, match="Another PaperDesk server .* is already using the data folder"):
        DataDirLock(str(tmp_path)).acquire()
    a.release()
    b = DataDirLock(str(tmp_path)).acquire()  # released: free again
    assert b.held
    b.release()


def test_a_lock_file_left_by_a_crash_does_not_block(tmp_path):
    (tmp_path / ".paperdesk.lock").write_bytes(b"")
    (tmp_path / ".paperdesk.lock.json").write_text(json.dumps({"pid": 999999, "host": "x", "started": time.time()}))
    lock = DataDirLock(str(tmp_path)).acquire()
    assert lock.held and not lock.fallback
    lock.release()


HOLDER = "import sys, time; sys.path.insert(0, sys.argv[2]); from app.storage.lock import DataDirLock; " \
         "L = DataDirLock(sys.argv[1]).acquire(); print('held', flush=True); time.sleep(60)"


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL")
def test_lock_held_by_another_process_and_freed_when_it_is_killed(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path), BACKEND], stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b"held"
        with pytest.raises(DataDirLocked) as e:
            DataDirLock(str(tmp_path)).acquire()
        assert f"process {p.pid}" in str(e.value) and "PAPERDESK_DATA" in str(e.value)
    finally:
        p.kill()
        p.wait(10)
    lock = DataDirLock(str(tmp_path)).acquire()  # the OS dropped the dead process's lock
    lock.release()


def test_fallback_rule_where_files_cannot_be_locked(tmp_path, monkeypatch):
    import errno
    import socket

    def no_locks(self, fh):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(DataDirLock, "_os_lock", no_locks)
    now = [1000.0]
    a = DataDirLock(str(tmp_path), wall=lambda: now[0]).acquire()
    assert a.fallback
    with pytest.raises(DataDirLocked):
        DataDirLock(str(tmp_path), wall=lambda: now[0]).acquire()  # same host, live pid, fresh heartbeat
    # a dead process on this host: stale
    (tmp_path / ".paperdesk.lock.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "host": socket.gethostname(), "started": now[0], "heartbeat": now[0]}))
    DataDirLock(str(tmp_path), wall=lambda: now[0]).acquire().release()
    # another host that stopped refreshing its heartbeat: stale after STALE_SECONDS
    (tmp_path / ".paperdesk.lock.json").write_text(json.dumps(
        {"pid": 1, "host": "elsewhere", "started": now[0], "heartbeat": now[0]}))
    with pytest.raises(DataDirLocked):
        DataDirLock(str(tmp_path), wall=lambda: now[0] + 10).acquire()
    DataDirLock(str(tmp_path), wall=lambda: now[0] + 601).acquire().release()


def test_server_refuses_to_start_on_a_folder_in_use(tmp_path, monkeypatch):
    from app.api import server
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path))
    monkeypatch.setenv("PAPERDESK_FEED", "synthetic")
    held = DataDirLock(str(tmp_path)).acquire()
    try:
        with pytest.raises(DataDirLocked):
            server.default_exchange()
        with pytest.raises(SystemExit) as e:
            server.app_factory()
        assert e.value.code == 1
    finally:
        held.release()
    ex = server.default_exchange()
    try:
        assert ex.data_lock.held and len(ex.accounts) == 2
    finally:
        ex.data_lock.release()
        ex.feed.close()


@pytest.mark.skipif(os.name != "posix", reason="process signals")
def test_second_server_process_exits_with_a_clear_message(tmp_path):
    env = {**os.environ, "PAPERDESK_DATA": str(tmp_path), "PAPERDESK_FEED": "synthetic",
           "PAPERDESK_BACKUP_INTERVAL": "0"}
    for k in ("PAPERDESK_LAN", "PAPERDESK_PASSCODE"):
        env.pop(k, None)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path), BACKEND], stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == b"held"
        r = subprocess.run([sys.executable, "-m", "uvicorn", "app.api.server:app_factory", "--factory", "--port", "0"],
                           cwd=BACKEND, env=env, capture_output=True, timeout=60)
        assert r.returncode != 0
        assert b"PaperDesk did not start: Another PaperDesk server" in r.stderr
    finally:
        holder.kill()
        holder.wait(10)
