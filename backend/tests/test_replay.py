"""Replay mode: session format, recorder, builder, ReplayFeed, controller and mode switching."""
import gzip
import io
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.core.models import to_plain
from app.exchange import Exchange
from app.marketdata.replay import ReplayFeed, SessionFormatError, SessionWriter, list_sessions, read_session
from app.marketdata.replay.__main__ import main as cli
from app.marketdata.replay.builder import build_from_bars, read_bars_csv
from app.marketdata.replay.control import MAX_TICKS_PER_ADVANCE, ReplayController
from app.marketdata.replay.format import SUFFIX, read_header
from app.marketdata.replay.library import BUNDLED_DIR, find_session
from app.marketdata.replay.recorder import record, session_times
from app.marketdata.replay.samples import SAMPLES
from app.marketdata.synthetic import SyntheticFeed

D = Decimal
DAY = date(2026, 3, 13)
OPEN = datetime.combine(DAY, cal.OPEN, tzinfo=cal.NY).timestamp()


def header(**kw):
    h = {"label": "Test", "date": DAY.isoformat(), "start": OPEN, "end": OPEN + 600, "simulated": True,
         "source": "test", "symbols": [{"symbol": "ABC", "name": "Abc Corp", "sector": "Test", "vol": 0.25}],
         "initial": {"ABC": {"bid": "99.99", "ask": "100.01", "bid_size": "500", "ask_size": "400", "last": "100.00",
                             "open": "100.00", "high": "100.00", "low": "100.00", "volume": "0", "prev_close": "98.50"}}}
    h.update(kw)
    return h


def write_tiny(path, events=None, close=True, **kw):
    w = SessionWriter(path)
    w.start(header(**kw))
    w.context("daily", "ABC", [[int(OPEN // 86400 * 86400) - 86400 * i, 98.0, 99.0, 97.0, 98.5, 1000] for i in (2, 1)])
    for e in events if events is not None else [
        ("Q", OPEN + 10, "ABC", D("100.10"), D("100.12"), D(300), D(200)),
        ("T", OPEN + 10, "ABC", D("100.12"), D(50)),
        ("T", OPEN + 20, "ABC", D("100.115"), D("0.5")),
        ("Q", OPEN + 30, "ABC", D("99.50"), D("99.52"), D(100), D(100)),
        ("T", OPEN + 40, "ABC", D("99.50"), D(1000)),
    ]:
        (w.quote if e[0] == "Q" else w.trade)(*e[1:])
    if close:
        w.close(OPEN + 600)
    return path


@pytest.fixture
def tiny(tmp_path):
    return str(write_tiny(tmp_path / ("tiny" + SUFFIX)))


def sample(i=0):
    return os.path.join(BUNDLED_DIR, SAMPLES[i][0] + SUFFIX)


# ---- format ---------------------------------------------------------------------
def test_format_round_trip_is_exact(tiny, tmp_path):
    s = read_session(tiny)
    assert s.header["version"] == 1 and s.symbols == ["ABC"] and s.start == OPEN and s.end == OPEN + 600
    assert s.events[0] == (OPEN + 10, "Q", "ABC", D("100.10"), D("100.12"), D(300), D(200))
    assert s.events[2] == (OPEN + 20, "T", "ABC", D("100.115"), D("0.5"))  # sub-cent and fractional sizes survive
    assert len(s.daily["ABC"]) == 2 and s.initial("ABC")["prev_close"] == D("98.50")
    again = write_tiny(tmp_path / ("again" + SUFFIX))
    assert open(tiny, "rb").read() == open(again, "rb").read()  # byte-identical for identical input
    with gzip.open(tiny, "rt", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert json.loads(lines[0])["format"] == "paperdesk-session" and json.loads(lines[-1])["end"]["events"] == 5


@given(st.lists(st.tuples(st.integers(0, 500), st.decimals("0.0001", "99999", places=4),
                          st.decimals("0.0001", "100000", places=4)), max_size=30))
@settings(max_examples=40, deadline=None)
def test_format_round_trip_property(rows):
    buf = io.BytesIO()
    w = SessionWriter(buf)
    w.start(header())
    rows = sorted(rows, key=lambda r: r[0])
    for t, px, size in rows:
        w.trade(OPEN + t, "ABC", px, size)
    w.close()
    path = os.path.join(tempfile.gettempdir(), f"pd-prop-{os.getpid()}{SUFFIX}")
    try:
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        got = read_session(path).events
    finally:
        os.remove(path)
    assert [(e[0], e[3], e[4]) for e in got] == [(OPEN + t, px, size) for t, px, size in rows]


def test_format_rejects_bad_files(tmp_path):
    cut = write_tiny(tmp_path / ("cut" + SUFFIX), close=False)
    with pytest.raises(SessionFormatError, match="incomplete"):
        read_session(cut)
    with pytest.raises(SessionFormatError, match="version 9"):
        write_tiny(tmp_path / ("v9" + SUFFIX), version=9)
    with pytest.raises(SessionFormatError, match="time order"):
        write_tiny(tmp_path / ("ord" + SUFFIX), events=[("T", OPEN + 9, "ABC", D(1), D(1)), ("T", OPEN + 8, "ABC", D(1), D(1))])
    with pytest.raises(SessionFormatError, match="starting quote"):
        write_tiny(tmp_path / ("noinit" + SUFFIX), initial={})
    newer = tmp_path / ("newer" + SUFFIX)
    with gzip.open(newer, "wt", encoding="utf-8") as f:
        f.write(json.dumps({**header(), "format": "paperdesk-session", "version": 2}) + "\n")
    with pytest.raises(SessionFormatError, match="reads up to version 1"):
        read_header(newer)
    junk = tmp_path / ("junk" + SUFFIX)
    junk.write_bytes(b"not gzip")
    with pytest.raises(SessionFormatError):
        read_header(junk)


# ---- feed -------------------------------------------------------------------------
def test_feed_plays_events_in_time_order(tiny):
    f = ReplayFeed.from_file(tiny)
    q0 = f.quote("ABC")
    assert (q0.bid, q0.ask, q0.last, q0.prev_close, q0.volume) == (D("99.99"), D("100.01"), D("100.00"), D("98.50"), 0)
    assert f.step(OPEN + 9) == []
    out = f.step(OPEN + 20)
    # quote and print at the same moment are one update; then a print on the standing book
    assert [(q.ts, q.trade, q.bid, q.last) for q in out] == [
        (OPEN + 10, True, D("100.10"), D("100.12")), (OPEN + 20, True, D("100.10"), D("100.115"))]
    assert f.quote("ABC").volume == D("50.5") and f.quote("ABC").high == D("100.12")
    assert [t["price"] for t in f.trades("ABC")] == [D("100.115"), D("100.12")]
    with pytest.raises(ValueError, match="back in time"):
        f.step(OPEN + 15)
    f.step(OPEN + 600)
    assert f.finished and f.quote("ABC").low == D("99.50") and f.progress(OPEN + 600)["fraction"] == 1.0
    assert f.bars("ABC", "1m")[-1]["close"] == 99.5 and len(f.bars("ABC", "1d")) == 3
    d1, d2 = f.depth("ABC"), ReplayFeed.from_file(tiny).depth("ABC")
    assert d1["simulated"] and len(d1["bids"]) == 10


def test_recording_a_provider_reproduces_its_quotes(tmp_path):
    """Record the synthetic feed, then replay: every quote the feed produced comes back identically."""
    def feed():
        return SyntheticFeed(seed=5, now=OPEN, history_days=30, always_open=False)
    times = session_times(DAY, 5.0)[:120]
    path = tmp_path / ("rec" + SUFFIX)
    stats = record(feed(), path, times, symbols=["NVLX", "ORBT"], label="rec")
    assert stats["events"] > 100
    live, rep = feed(), ReplayFeed.from_file(path)
    fields = ("bid", "ask", "bid_size", "ask_size", "last", "volume", "high", "low", "prev_close", "open")
    for t in times:
        live.step(t)
        rep.step(t)
        for s in ("NVLX", "ORBT"):
            a, b = live.quote(s), rep.quote(s)
            assert [getattr(a, k) for k in fields] == [getattr(b, k) for k in fields], (t, s)
    assert rep.info["NVLX"]["name"] == live.info["NVLX"]["name"]
    assert len(rep.store.daily["NVLX"]) == 30 and rep.store.daily["NVLX"] == live.store.daily["NVLX"]


def test_builder_turns_bars_into_a_plausible_stream(tmp_path):
    csv_path = tmp_path / "bars.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write("symbol,time,open,high,low,close,volume\n")
        f.write("abc,2026-03-13T09:30:00,10.00,10.50,9.80,10.40,1000\n")   # up bar
        f.write("ABC,2026-03-13T09:31:00,10.40,10.45,10.10,10.20,402\n")   # down bar
        f.write("ABC,2026-03-13T09:32:00,10.20,10.20,10.20,10.20,0\n")    # no trades
    daily_path = tmp_path / "daily.csv"
    with open(daily_path, "w", encoding="utf-8", newline="") as f:
        f.write("symbol,time,open,high,low,close,volume\nABC,2026-03-12,9.5,9.9,9.4,9.75,50000\n")
    out = tmp_path / ("built" + SUFFIX)
    assert cli(["build", "--bars", str(csv_path), "--daily", str(daily_path), "--out", str(out)]) == 0
    s = read_session(out)
    trades = [e for e in s.events if e[1] == "T"]
    assert [e[3] for e in trades] == [D("10.00"), D("9.80"), D("10.50"), D("10.40"), D("10.40"), D("10.45"), D("10.10"), D("10.20")]
    assert [e[0] - OPEN for e in trades] == [0, 15, 30, 45, 60, 75, 90, 105]
    assert sum(e[4] for e in trades[:4]) == 1000 and sum(e[4] for e in trades[4:]) == 402
    assert s.initial("ABC")["prev_close"] == D("9.75") and s.header["simulated"] is False
    f = ReplayFeed(s)
    f.step(s.end)
    q = f.quote("ABC")
    assert q.bid < q.ask and q.high == D("10.50") and q.low == D("9.80") and q.volume == 1402
    assert f.bars("ABC", "1m")[0] == {"time": int(OPEN), "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.4, "volume": 1000.0}
    with pytest.raises(ValueError, match="dates"):
        build_from_bars(tmp_path / "x", {"ABC": [[int(OPEN), 1, 1, 1, 1, 1], [int(OPEN) + 86400, 1, 1, 1, 1, 1]]})
    assert read_bars_csv(csv_path)["ABC"][0][0] == int(OPEN)


def test_cli_record_and_inspect(tmp_path, capsys):
    out = tmp_path / "cli"
    assert cli(["record", "--date", "2026-03-13", "--seed", "3", "--symbols", "NVLX", "--step", "30", "--out", str(out)]) == 0
    path = str(out) + SUFFIX
    assert cli(["inspect", path]) == 0
    text = capsys.readouterr().out
    assert "simulated data" in text and "NVLX" in text and "09:30:00 to 16:00:00" in text


def test_bundled_samples_are_small_labelled_and_reproducible(tmp_path):
    rows = list_sessions([BUNDLED_DIR])
    assert len(rows) == len(SAMPLES) >= 2
    assert sum(r["size_bytes"] for r in rows) < 1_500_000
    assert all(r["simulated"] and "imulated" in r["label"] and not r.get("error") for r in rows)
    from app.marketdata.replay.samples import build_sample
    sid, day, seed, note = SAMPLES[1]
    build_sample(sid, day, seed, note, out_dir=str(tmp_path))
    assert open(tmp_path / (sid + SUFFIX), "rb").read() == open(sample(1), "rb").read()


# ---- controller -------------------------------------------------------------------
def test_speed_pause_step_and_seek(tiny):
    c = ReplayController(tiny)
    t0 = c.now()
    assert c.paused and c.status()["progress"] == 0.0
    assert c.advance(10) == 0 and c.now() == t0  # paused: nothing moves
    c.play()
    c.set_speed(60)
    assert c.advance(0.25) == 15 and c.now() == t0 + 15
    c.set_speed(1)
    assert [c.advance(0.25) for _ in range(4)] == [0, 0, 0, 1]  # fractions carry over
    assert c.now() == t0 + 16
    c.set_speed(60)
    assert c.advance(1000) == MAX_TICKS_PER_ADVANCE and c.status()["lagging"]  # bounded work, debt dropped
    c.pause()
    assert c.step(1) == 1 and c.step(60) == 60
    with pytest.raises(ValueError):
        c.set_speed(61)
    with pytest.raises(ValueError):
        c.set_speed(0.5)
    with pytest.raises(ValueError, match="back in time"):
        c.seek(t0)
    assert c.seek(c.end + 999) > 0 and c.ended and c.paused and c.status()["progress"] == 1.0
    with pytest.raises(ValueError, match="ended"):
        c.play()
    with pytest.raises(ValueError, match="ended"):
        c.step()


def _script(c: ReplayController) -> str:
    """A fixed sequence of commands at fixed simulated times; returns the sandbox ledger as JSON text."""
    aid = list(c.ex.accounts)[0]
    c.step(120)
    c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 25})
    c.ex.submit(aid, {"symbol": "ORBT", "side": "buy", "qty": 10, "type": "limit",
                      "limit_price": str(c.feed.quote("ORBT").bid - D("0.50")), "tif": "day"})
    c.step(900)
    c.ex.submit(aid, {"symbol": "NVLX", "side": "sell", "qty": 10, "type": "stop",
                      "stop_price": str(c.feed.quote("NVLX").last - D("0.40")), "tif": "gtc"})
    c.ex.submit(aid, {"symbol": "AUTO", "side": "sell", "qty": 5})  # short
    c.play()
    c.set_speed(60)
    for _ in range(40):
        c.advance(0.5)
    c.seek(c.start + 3 * 3600)
    c.ex.submit(aid, {"symbol": "NVLX", "side": "sell", "qty": 15})
    c.seek(c.end)
    return json.dumps({"accounts": [a.to_plain() for a in c.ex.accounts.values()],
                       "orders": [to_plain(o) for o in c.ex.engine.orders.values()]}, sort_keys=True)


def test_deterministic_playback_gives_identical_ledgers(tmp_path):
    a = _script(ReplayController(sample(0), str(tmp_path / "a")))
    b = _script(ReplayController(sample(0), str(tmp_path / "b")))
    assert a == b
    assert '"kind": "trade"' in a or '"trade"' in a


def test_orders_fill_against_replayed_quotes_at_simulated_times():
    c = ReplayController(sample(0))
    aid = list(c.ex.accounts)[0]
    c.step(600)
    now = c.now()
    ask = c.feed.quote("NVLX").ask
    (o,) = c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 10})
    f = c.ex.account(aid).fills[-1]
    assert o.status == "filled" and f.ts == now and f.price == ask
    # A resting limit below the market fills at the first replayed moment the ask reaches it.
    limit = c.feed.quote("NVLX").ask - D("0.30")
    (lo,) = c.ex.submit(aid, {"symbol": "NVLX", "side": "buy", "qty": 5, "type": "limit", "limit_price": str(limit), "tif": "gtc"})
    # Expected: the first replayed NVLX update after `now` whose ask reaches the limit or that prints below it.
    ref = ReplayFeed.from_file(sample(0))
    ref.step(now)
    first = next(((q.ts,) for q in ref.step(c.end) if q.symbol == "NVLX"
                  and (q.ask <= limit or (q.trade and q.last < limit))), None)
    c.seek(c.end)
    if first is None:
        assert lo.status == "working"
    else:
        lf = [x for x in c.ex.account(aid).fills if x.order_id == lo.id]
        assert lf and lf[0].ts == first[0] and lf[0].price <= limit


def test_session_rules_follow_the_simulated_clock():
    # Early-close day: a day order still working at 1:00 pm expires then, not at 4:00 pm.
    c = ReplayController(sample(1), account_type="cash", starting_cash=D(50000))
    aid = list(c.ex.accounts)[0]
    assert c.ex.account(aid).type == "cash" and aid == "A9001"
    (o,) = c.ex.submit(aid, {"symbol": "MKTX", "side": "buy", "qty": 1, "type": "limit", "limit_price": "1"})
    assert c.ex.session() == "regular"
    c.seek(c.end - 1)
    assert o.status == "working"
    c.step(1)
    assert o.status == "expired" and o.updated_at == cal.close_ts(date(2025, 11, 28))
    # Friday: an option expiring today is settled at the simulated close.
    c = ReplayController(sample(0))
    aid = list(c.ex.accounts)[0]
    from app.options import chain as optchain
    spot = float(c.feed.quote("NVLX").last)
    exp = optchain.expirations(DAY)[0]
    assert exp == DAY
    row = min(optchain.chain("NVLX", exp, spot, c.feed.base_vol("NVLX"), c.now()), key=lambda r: abs(float(r["strike"]) - spot))
    c.ex.submit(aid, {"symbol": row["call"]["symbol"], "side": "buy", "qty": 1})
    assert any(p["kind"] == "option" for p in c.ex.positions(aid))
    c.seek(c.end)
    kinds = {f.kind for f in c.ex.account(aid).fills}
    assert kinds & {"exercise", "expiration"}
    assert not any(p["kind"] == "option" for p in c.ex.positions(aid))
    assert all(f.ts == cal.close_ts(DAY) for f in c.ex.account(aid).fills if f.kind != "trade")


# ---- server and API ------------------------------------------------------------------
def live_client(tmp_path, run_loop=False, **kw):
    import time
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path / "live"))
    ex.create_account("Margin", "margin")
    ex.tick()
    return TestClient(create_app(ex, run_loop=run_loop, **kw)), ex


def snapshot(ex):
    return json.dumps({"accounts": [a.to_plain() for a in ex.accounts.values()],
                       "orders": [to_plain(o) for o in ex.engine.orders.values()],
                       "watchlists": ex.watchlists, "alerts": ex.alerts}, sort_keys=True)


def test_api_replay_flow_and_live_state_untouched(tmp_path):
    c, live = live_client(tmp_path)
    aid = list(live.accounts)[0]
    c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 3})
    c.post(f"/api/accounts/{aid}/orders", json={"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "limit_price": 1, "tif": "gtc"})
    live.save(force=True)
    before_mem = snapshot(live)
    before_disk = open(tmp_path / "live" / "state.json", "rb").read()

    assert c.get("/api/replay/status").json() == {"active": False}
    assert c.post("/api/replay/pause").status_code == 409
    sessions = c.get("/api/replay/sessions").json()
    assert {s["id"] for s in sessions} >= {s[0] for s in SAMPLES}
    assert c.post("/api/replay/start", json={"session": "nope"}).status_code == 404
    assert c.post("/api/replay/start", json={"session": "../../etc/passwd"}).status_code == 404
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0], "account_type": "gold"}).status_code == 400
    r = c.post("/api/replay/start", json={"session": SAMPLES[0][0], "starting_cash": 20000, "account_type": "margin"})
    assert r.status_code == 200 and r.json()["active"] and r.json()["paused"]
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 409

    st = c.get("/api/status").json()
    assert st["mode"] == "replay" and st["replay"]["date"] == "2026-03-13" and st["time"] == OPEN
    assert st["feed"] == "replay" and st["simulated_data"] and st["session"] == "regular"
    accts = c.get("/api/accounts").json()
    assert [a["id"] for a in accts] == ["A9001"] and accts[0]["cash"] == 20000
    assert set(c.get("/api/watchlists").json()["Main"]) == {"MKTX", "TECX", "NVLX", "ORBT", "AUTO", "FNBK", "ENRG"}
    assert c.get("/api/bars/PXLR").status_code == 404  # not in the session
    assert len(c.get("/api/bars/NVLX", params={"tf": "1d"}).json()) == 261
    exps = c.get("/api/options/expirations/NVLX").json()
    assert exps[0]["date"] == "2026-03-13"
    assert c.get("/api/options/chain/NVLX", params={"expiry": exps[0]["date"]}).json()["rows"]
    assert c.get("/api/depth/NVLX").json()["simulated"]

    assert c.post("/api/replay/step", json={"seconds": 90}).json()["time"] == OPEN + 90
    r = c.post("/api/accounts/A9001/orders", json={"symbol": "NVLX", "side": "buy", "qty": 10}).json()
    assert r[0]["status"] == "filled" and r[0]["created_at"] == OPEN + 90
    assert c.get("/api/trades/NVLX").json()[0]["ts"] <= OPEN + 90
    assert c.get("/api/bars/NVLX", params={"tf": "1m"}).json()[-1]["time"] == OPEN + 60
    assert c.post("/api/replay/speed", json={"speed": 30}).json()["speed"] == 30
    assert c.post("/api/replay/speed", json={"speed": 100}).status_code == 400
    assert c.post("/api/replay/resume").json()["paused"] is False
    assert c.post("/api/replay/pause").json()["paused"] is True
    assert c.post("/api/replay/seek", json={"offset": 3600}).json()["time"] == OPEN + 3600
    assert c.post("/api/replay/seek", json={"time": OPEN}).status_code == 400
    assert c.post("/api/replay/seek", json={}).status_code == 400
    assert c.post("/api/replay/step", json={"seconds": 0}).status_code == 400
    assert c.get("/api/accounts/A9001/positions").json()[0]["qty"] == 10
    assert c.get(f"/api/accounts/{aid}").status_code == 404  # live accounts are not visible in replay
    assert os.path.isdir(tmp_path / "live" / "replay")

    assert c.post("/api/replay/exit").json() == {"active": False}
    assert c.post("/api/replay/exit").status_code == 409
    st = c.get("/api/status").json()
    assert st["mode"] == "live" and st["replay"] is None and st["feed"] == "synthetic"
    assert snapshot(live) == before_mem
    assert open(tmp_path / "live" / "state.json", "rb").read() == before_disk
    assert [a["id"] for a in c.get("/api/accounts").json()] == [aid]
    # a second replay starts from a clean sandbox
    c.post("/api/replay/start", json={"session": SAMPLES[2][0]})
    assert c.get("/api/accounts/A9001/positions").json() == []
    c.post("/api/replay/exit")
    assert snapshot(live) == before_mem


def test_user_recorded_sessions_are_listed(tmp_path):
    c, live = live_client(tmp_path)
    write_tiny(tmp_path / "live" / "sessions" / ("mine" + SUFFIX))
    (tmp_path / "live" / "sessions" / ("broken" + SUFFIX)).write_bytes(b"junk")
    rows = {r["id"]: r for r in c.get("/api/replay/sessions").json()}
    assert rows["mine"]["bundled"] is False and rows["broken"]["error"]
    assert find_session([str(tmp_path / "live" / "sessions")], "mine").endswith("mine" + SUFFIX)
    assert c.post("/api/replay/start", json={"session": "broken"}).status_code == 422
    assert c.post("/api/replay/start", json={"session": "mine"}).status_code == 200


def test_tick_loop_drives_the_replay_clock_and_stream(tmp_path):
    c, live = live_client(tmp_path, run_loop=True, tick_seconds=0.02, replay_tick_seconds=0.02)
    with c:
        c.post("/api/replay/start", json={"session": SAMPLES[0][0], "speed": 60, "play": True})
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"symbols": ["NVLX"], "account": "A9001"})
            times = []
            for _ in range(200):
                msg = ws.receive_json()
                if msg["quotes"] and msg.get("account"):
                    times.append(msg["status"]["time"])
                if len(times) >= 5:
                    break
            assert msg["status"]["mode"] == "replay" and msg["account"]["id"] == "A9001"
            assert times == sorted(times) and times[-1] > OPEN
            assert OPEN <= msg["quotes"]["NVLX"]["ts"] <= msg["status"]["time"]
        c.post("/api/replay/exit")
        assert c.get("/api/status").json()["mode"] == "live"


def test_realtime_recording_paces_by_wall_clock(tmp_path):
    from app.marketdata.replay.recorder import realtime
    t = [1000.0]
    slept = []
    times = list(realtime(3, 1.0, sleep=lambda d: (slept.append(d), t.__setitem__(0, t[0] + d)), clock=lambda: t[0]))
    assert times == [1000.0, 1001.0, 1002.0, 1003.0] and slept == [1.0, 1.0, 1.0]
    feed = SyntheticFeed(seed=2, now=OPEN, history_days=5)
    stats = record(feed, tmp_path / ("rt" + SUFFIX), [OPEN + i for i in range(5)], symbols=["NVLX"])
    s = read_session(tmp_path / ("rt" + SUFFIX))
    assert stats["events"] == len(s.events) > 0 and s.end == OPEN + 4 and s.header["end"] is None
