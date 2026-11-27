"""Stress tests: many threads use the API while the server loop ticks at full speed.

Each test is seeded and bounded. They fail on a server without the exchange lock (see docs/CONCURRENCY.md)
and are the regression suite for it. PAPERDESK_STRESS=1 runs a much longer variant of each.
"""
import os
import threading

from stress import D, T0, Rig, file_problems, load_state, problems, scaled, trader

from app.marketdata.feed import SimFeed
from app.marketdata.replay.library import BUNDLED_DIR


def check_everything(rig: Rig, capsys, state_findings=()):
    """After the server has shut down: no failed request, no tick fault, consistent books, consistent file."""
    out = capsys.readouterr().out
    assert "tick error" not in out, out[-2000:]
    m = rig.ctx.metrics
    # Who held the lock longest, and how long the ticks waited and ran: names the holder when requests timed out.
    assert not rig.failures, (len(rig.failures), rig.failures[:5], "longest hold", rig.ctx.lock.longest,
                              "tick wait max", m.get("lock_wait_max"), "turn max", m.get("turn_max"))
    assert not list(state_findings), list(state_findings)[:10]
    assert problems(rig.ex) == []
    path = os.path.join(rig.data, "state.json")
    assert file_problems(path, os.path.join(rig.tmp, "final")) == []
    with open(path, "rb") as f:
        saved = f.read()
    rig.ex.save(force=True)  # what is on disk is exactly the final state: saving again changes nothing
    with open(path, "rb") as f:
        assert f.read() == saved


def test_orders_accounts_and_settings_from_many_threads_while_the_market_ticks(tmp_path, capsys):
    workers, requests = scaled(6, 8), scaled(60, 500)
    rig = Rig(tmp_path, seed=11, accounts=(("margin", "5000000"), ("margin", "300000"), ("cash", "200000")))
    steady, chaos, cash = rig.accounts
    stop, found = threading.Event(), []
    with rig:
        watcher = threading.Thread(target=rig.watch_state_file, args=(stop, found, "watch"), daemon=True)
        watcher.start()
        rig.run([trader(rig, 100 + i, requests, steady, chaos if i % 2 else cash) for i in range(workers)])
        stop.set()
        watcher.join(30)
    assert rig.ticks > 20, "the market did not tick while the requests ran"
    check_everything(rig, capsys, found)

    # No order id was lost or handed out twice: every accepted order is still there, exactly once.
    ex = rig.ex
    seen = {}
    for client_id, ids in rig.accepted:
        for oid in ids:
            assert oid in ex.engine.orders, f"accepted order {oid} is gone"
            assert ex.engine.orders[oid].account_id == steady
            if client_id is None:
                assert oid not in seen, f"order id {oid} was returned by two different requests"
            seen[oid] = client_id
    # A cancel that succeeded is final.
    for oid in rig.canceled:
        assert ex.engine.orders[oid].status == "canceled", (oid, ex.engine.orders[oid].status)
    # The saved file holds the same orders and fills as memory.
    loaded = load_state(os.path.join(rig.data, "state.json"), os.path.join(rig.tmp, "reload"))
    assert set(loaded.engine.orders) == set(ex.engine.orders)
    assert {a: [f.id for f in acct.fills] for a, acct in loaded.accounts.items()} == \
           {a: [f.id for f in acct.fills] for a, acct in ex.accounts.items()}


def test_a_double_click_with_one_client_id_makes_one_order(tmp_path, capsys):
    rounds, clicks = scaled(16, 150), 8
    rig = Rig(tmp_path, seed=5)
    aid = rig.accounts[0]
    with rig:
        for i in range(rounds):
            body = {"symbol": "MKTX", "side": "buy", "qty": 1, "client_id": f"click-{i}"}
            if i % 2:
                body.update(type="limit", tif="gtc", limit_price="1.00")
            gate, got = threading.Barrier(clicks), []

            def click():
                gate.wait()
                r = rig.call("POST", f"/api/accounts/{aid}/orders", json=body)
                got.append(r.json()[0]["id"] if r.status_code == 200 else r.status_code)

            rig.run([click] * clicks)
            assert len(set(got)) == 1, f"round {i}: one client id returned {sorted(set(map(str, got)))}"
            made = [o.id for o in rig.ex.engine.orders.values() if o.client_id == body["client_id"]]
            assert len(made) == 1, f"round {i}: client id made orders {made}"
    check_everything(rig, capsys)
    assert len(rig.ex.engine.orders) == rounds


def test_cancel_against_fill_ends_in_exactly_one_terminal_state(tmp_path, capsys):
    rounds, per_round = scaled(16, 150), 8
    feed = SimFeed()
    feed.set_quote("ABC", D("100.49"), D("100.50"), D("100.50"))
    rig = Rig(tmp_path, feed=feed, step=1.0)
    aid = rig.accounts[0]
    results = {}
    with rig:
        for i in range(rounds):
            ids = [rig.call("POST", f"/api/accounts/{aid}/orders", json={
                "symbol": "ABC", "side": "buy", "qty": 10, "type": "limit", "tif": "gtc", "limit_price": "100.00"}).json()[0]["id"]
                for _ in range(per_round)]
            gate = threading.Barrier(per_round + 1)

            def cancel(oid):
                def go():
                    gate.wait()
                    results[oid] = rig.call("DELETE", f"/api/accounts/{aid}/orders/{oid}").status_code
                return go

            def cross():  # the market trades down through the limit while the cancels arrive
                gate.wait()
                feed.set_quote("ABC", D("99.98"), D("99.99"), D("99.99"), ask_size=35)
                feed.set_quote("ABC", D("99.97"), D("99.98"), D("99.98"), ask_size=35)

            rig.run([cancel(oid) for oid in ids] + [cross])
            feed.set_quote("ABC", D("100.49"), D("100.50"), D("100.50"))
            rig.call("DELETE", f"/api/accounts/{aid}/orders")
    check_everything(rig, capsys)
    ex = rig.ex
    filled = D(0)
    for oid, status in results.items():
        o = ex.engine.orders[oid]
        got = sum((f.qty for f in ex.accounts[aid].fills if f.order_id == oid), D(0))
        assert o.status in ("canceled", "filled"), (oid, o.status)
        assert got == o.filled_qty, f"{oid}: {o.status}, filled {o.filled_qty}, fills {got}"
        if status == 200:
            assert o.status == "canceled" and o.filled_qty < o.qty, f"{oid}: cancel succeeded but the order is {o.status}"
        else:
            assert status == 400 and o.status == "filled" and o.filled_qty == o.qty, (oid, status, o.status)
        filled += o.filled_qty
    assert ex.accounts[aid].position("ABC") == filled


def test_reset_and_delete_during_working_orders_and_liquidation(tmp_path, capsys):
    rounds = scaled(12, 100)
    feed = SimFeed()
    feed.set_quote("ABC", D("99.99"), D("100.00"), D("100.00"), bid_size=100000, ask_size=100000)
    feed.set_quote("XYZ", D("49.99"), D("50.00"), D("50.00"), bid_size=100000, ask_size=100000)
    rig = Rig(tmp_path, feed=feed, step=3600.0, accounts=(("margin", "100000"), ("margin", "100000"), ("cash", "5000")))
    target, spare, keep = rig.accounts
    with rig:
        for i in range(rounds):
            feed.set_quote("ABC", D("99.99"), D("100.00"), D("100.00"), bid_size=100000, ask_size=100000)
            for aid in (target, spare):
                rig.call("PUT", f"/api/accounts/{aid}/settings", json={"settings": {"margin_call_grace_hours": "0"}})
                rig.call("POST", f"/api/accounts/{aid}/orders", json={"symbol": "ABC", "side": "buy", "qty": 1900})
                for px in ("90.00", "80.00"):
                    rig.call("POST", f"/api/accounts/{aid}/orders", json={
                        "symbol": "XYZ", "side": "buy", "qty": 5, "type": "limit", "tif": "gtc", "limit_price": px})
            gate = threading.Barrier(5)

            def crash():  # the price collapses: margin calls, then forced selling over the next ticks
                gate.wait()
                for px in ("60.00", "45.00", "30.00"):
                    feed.set_quote("ABC", D(px) - D("0.01"), D(px), D(px), bid_size=300, ask_size=300)

            def reset():
                gate.wait()
                rig.call("POST", f"/api/accounts/{target}/reset", json={})
                rig.call("POST", f"/api/accounts/{target}/orders", json={"symbol": "XYZ", "side": "buy", "qty": 1})

            def delete():
                gate.wait()
                rig.call("DELETE", f"/api/accounts/{spare}")

            def trade():
                gate.wait()
                for aid in (target, spare, keep):
                    rig.call("POST", f"/api/accounts/{aid}/orders", json={"symbol": "ABC", "side": "sell", "qty": 10})
                    rig.call("DELETE", f"/api/accounts/{aid}/orders")
                    rig.call("GET", f"/api/accounts/{aid}")

            rig.run([crash, reset, delete, trade, trade])
            spare = rig.call("POST", "/api/accounts", json={"name": "spare", "type": "margin", "starting_cash": 100000}).json()["id"]
            rig.call("POST", f"/api/accounts/{target}/reset", json={})
    check_everything(rig, capsys)
    assert set(rig.ex.accounts) == {target, spare, keep}
    assert all(o.account_id in rig.ex.accounts for o in rig.ex.engine.orders.values()), "an order outlived its account"


def test_replay_enter_step_and_exit_under_load_leave_live_state_byte_identical(tmp_path, capsys):
    rounds, requests = scaled(3, 20), scaled(25, 300)
    rig = Rig(tmp_path, seed=3)
    live_id, ctx = rig.accounts[0], None
    session = sorted(f for f in os.listdir(BUNDLED_DIR) if f.endswith(".gz"))[0].split(".")[0]
    state_path = os.path.join(rig.data, "state.json")
    mismatches, done = [], threading.Event()
    with rig:
        ctx = rig.ctx
        for s in ("MKTX", "TECX"):
            rig.call("POST", f"/api/accounts/{live_id}/orders", json={
                "symbol": s, "side": "buy", "qty": 3, "type": "limit", "tif": "gtc", "limit_price": "1.00"})
        leave = ctx.exit_replay

        def checked_exit():
            # At the moment of leaving, live must be exactly what was saved when the replay began.
            with ctx.lock:
                sandbox = ctx.ex
                with open(state_path, "rb") as f:
                    at_enter = f.read()
                rig.ex.save(force=True)
                with open(state_path, "rb") as f:
                    if f.read() != at_enter:
                        mismatches.append("live state changed while the replay ran")
                mismatches.extend(f"sandbox: {p}" for p in problems(sandbox))
                return leave()

        ctx.exit_replay = checked_exit

        def switcher():
            for i in range(rounds):
                r = rig.call("POST", "/api/replay/start", json={"session": session, "play": bool(i % 2), "speed": 60})
                assert r.status_code == 200, r.text
                for _ in range(4):
                    rig.call("POST", "/api/replay/step", json={"seconds": 120})
                rig.call("POST", "/api/replay/seek", json={"offset": 1800 + 600 * i})
                rig.call("POST", "/api/replay/pause")
                assert rig.call("POST", "/api/replay/exit").status_code == 200
                for _ in range(8):
                    rig.call("GET", "/api/status")
            done.set()

        def worker(seed):
            def go():
                n = 0
                while not done.is_set() or n < requests:
                    n += 1
                    for aid in (live_id, "A9001"):  # live account, replay sandbox account
                        r = rig.call("POST", f"/api/accounts/{aid}/orders", json={
                            "symbol": "MKTX", "side": "buy", "qty": 1 + (n + seed) % 3})
                        if r.status_code == 200:
                            assert all(o["account_id"] == aid for o in r.json())
                        rig.call("GET", f"/api/accounts/{aid}")
                        rig.call("POST", "/api/replay/step", json={"seconds": 5})
                    if n > 200000:
                        break
            return go

        rig.run([switcher] + [worker(i) for i in range(4)])
        assert ctx.mode == "live"
    assert not mismatches, mismatches[:5]
    check_everything(rig, capsys)
