"""Disk and long-uptime behaviour: damaged state files, failing writes, growth of what is saved.

The tests that describe behaviour the app does not have yet are xfail(strict=True) with the
reason; see tests/soak/test_findings.py for how that works.
"""
import errno
import json
import os

import pytest

from app.api import server
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from helpers import D, T0


def live(tmp_path):
    ex = Exchange(SyntheticFeed(seed=3, now=T0, history_days=20), SimClock(T0), data_dir=str(tmp_path))
    a = ex.create_account("Margin", "margin")
    ex.tick()
    ex.submit(a.id, {"symbol": "NVLX", "side": "buy", "qty": 10})
    ex.save(force=True)
    return ex, a


def start_app_exchange(tmp_path, monkeypatch):
    """What the server does at start-up (server.default_exchange), against this data directory."""
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path))
    monkeypatch.setenv("PAPERDESK_FEED", "synthetic")
    monkeypatch.setenv("PAPERDESK_ENV_FILE", str(tmp_path / "no.env"))
    for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "FINNHUB_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env from the developer's checkout
    return server.default_exchange()


def damaged_variants(text: str) -> dict:
    data = json.loads(text)
    no_seq = dict(data)
    no_seq.pop("seq")
    return {"truncated": text[: len(text) // 2], "empty": "", "not_json": "\x00\x00garbage",
            "missing_key": json.dumps(no_seq)}


def test_a_good_state_file_loads_at_start_up(tmp_path, monkeypatch):
    ex, a = live(tmp_path)
    ex2 = start_app_exchange(tmp_path, monkeypatch)
    assert ex2.account(a.id).positions() == {"NVLX": D(10)}


@pytest.mark.xfail(strict=True, reason=(
    "exchange.py load(): a state.json that is not valid JSON (truncated, empty, garbage) makes load() return "
    "False silently; server.default_exchange then creates fresh default accounts and the next save() replaces "
    "the damaged file, so every account, order and fill is lost with no copy kept and nothing said"))
@pytest.mark.parametrize("variant", ["truncated", "empty", "not_json"])
def test_a_damaged_state_file_is_kept_and_reported(tmp_path, monkeypatch, capsys, caplog, variant):
    ex, a = live(tmp_path)
    path = tmp_path / "state.json"
    bad = damaged_variants(path.read_text())[variant]
    path.write_text(bad)
    ex2 = start_app_exchange(tmp_path, monkeypatch)
    ex2.save(force=True)
    kept = [p for p in tmp_path.iterdir() if p.is_file() and p.read_bytes() == bad.encode()]
    said = capsys.readouterr()
    told = "state.json" in (said.out + said.err + caplog.text)
    assert kept and told, f"damaged file kept: {bool(kept)}, reported: {told}"


@pytest.mark.xfail(strict=True, reason=(
    "exchange.py load(): valid JSON missing a required key (here 'seq') raises KeyError out of "
    "server.default_exchange, so the server does not start; the message names neither the file nor the problem"))
def test_a_state_file_with_a_missing_key_does_not_stop_start_up(tmp_path, monkeypatch):
    ex, a = live(tmp_path)
    path = tmp_path / "state.json"
    path.write_text(damaged_variants(path.read_text())["missing_key"])
    start_app_exchange(tmp_path, monkeypatch)


def test_a_damaged_market_file_is_ignored_and_accounts_still_load(tmp_path, monkeypatch):
    ex, a = live(tmp_path)
    (tmp_path / "market.json").write_text('{"quotes": {"NVLX": ')
    ex2 = start_app_exchange(tmp_path, monkeypatch)
    assert ex2.account(a.id).positions() == {"NVLX": D(10)}
    assert ex2.feed.quote("NVLX") is not None


def failing_disk(monkeypatch, err):
    import tempfile as tf

    def boom(*a, **k):
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(tf, "mkstemp", boom)


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EROFS, errno.EACCES])
def test_a_failed_save_raises_and_leaves_the_old_file_whole(tmp_path, monkeypatch, err):
    ex, a = live(tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    ex.create_account("Second", "cash", D("1000"))
    with monkeypatch.context() as mp:
        failing_disk(mp, err)
        with pytest.raises(OSError):
            ex.save(force=True)
    assert (tmp_path / "state.json").read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


@pytest.mark.xfail(strict=True, reason=(
    "exchange.py save(): `self.dirty = False` is set before the write, so when the disk is full or read-only "
    "the change is marked saved; the next save() (the tick loop never forces one) writes nothing until "
    "something else changes, and a crash in between loses it"))
def test_a_change_that_failed_to_save_is_saved_when_the_disk_recovers(tmp_path, monkeypatch):
    ex, a = live(tmp_path)
    b = ex.create_account("Second", "cash", D("1000"))
    with monkeypatch.context() as mp:
        failing_disk(mp, errno.ENOSPC)
        with pytest.raises(OSError):
            ex.save()
    ex.save()  # the disk works again: the tick loop's next periodic save
    ids = [x["id"] for x in json.loads((tmp_path / "state.json").read_text())["accounts"]]
    assert b.id in ids


def test_a_write_error_in_the_tick_loop_does_not_stop_the_market(tmp_path, monkeypatch, capsys):
    """The server loop catches the error, prints it and keeps ticking; requests keep working."""
    import time
    from fastapi.testclient import TestClient
    ex, a = live(tmp_path)
    ticks = []
    real_tick = ex.tick
    ex.tick = lambda: ticks.append(1) or real_tick()
    app = server.create_app(ex, run_loop=True, tick_seconds=0.01)
    with TestClient(app) as c:
        with monkeypatch.context() as mp:
            failing_disk(mp, errno.ENOSPC)
            ex.dirty = True
            deadline = time.monotonic() + 5
            while len(ticks) < 20 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(ticks) >= 20
            assert c.get(f"/api/accounts/{a.id}").status_code == 200
        # (the shutdown save would raise too while the disk is still full; the disk is back here)
    assert "tick error" in capsys.readouterr().out

