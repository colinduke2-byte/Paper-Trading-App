"""Live provider gaps: corporate actions applied from Alpaca, real option listings, missing prices,
symbol lists and replay sessions built from provider bars. No network: every response is a saved
documentation example (tests/fixtures/SOURCES.txt), edited only where a test needs other dates or
symbols, served by a fake transport."""
import copy
import gzip
import io
import json
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core import calendar as cal
from app.core.clock import SimClock
from app.exchange import Exchange, OrderRejected
from app.lifecycle.actions import ActionsPending, ActionsUnavailable
from app.marketdata import alpaca as al
from app.marketdata.alpaca import AlpacaFeed, SymbolState
from app.marketdata.alpaca_actions import ActionBook, to_lifecycle
from app.marketdata.config import held_symbols
from app.marketdata.feed import SimFeed
from app.marketdata.research import corporate_action_records
from app.marketdata.replay.format import read_session
from app.marketdata.replay.provider import build_from_provider

from test_alpaca import FakeTransport, fixture, raw, routes, ts

D = Decimal
EXAMPLES = "alpaca/corporate_actions_examples.json"


def examples() -> dict:
    return copy.deepcopy(fixture(EXAMPLES))


def ny(day: str, hhmm: str = "00:00") -> float:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(date.fromisoformat(day), datetime.min.time().replace(hour=h, minute=m),
                            tzinfo=cal.NY).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


# ---- mapping Alpaca records ------------------------------------------------------------------------
def test_every_documented_record_kind_maps_to_an_action_or_a_notice():
    recs = corporate_action_records(examples()["corporate_actions"])
    acts, notes = to_lifecycle(recs)
    by = {a.symbol: a for a in acts}
    fcf = by["FCF"]  # cash_dividend example
    assert fcf.kind == "dividend" and fcf.amount == D("0.125") and fcf.ex_date == date(2023, 5, 4)
    assert fcf.pay_date == date(2023, 5, 19) and fcf.id == "alpaca:11cfd108-292e-4cc6-bfbf-5999cdbc4029"
    assert fcf.revision_key == "alpaca|FCF|cash_dividend|2023-05-04|regular"
    sre = by["SRE"]  # forward_split example: payable date before ex-date does not matter for a split
    assert (sre.kind, sre.new, sre.old, sre.ex_date) == ("split", 2, 1, date(2023, 8, 22))
    mnts = by["MNTS"]  # reverse_split example, 1 for 50
    assert (mnts.new, mnts.old) == (1, 50) and mnts.is_reverse
    gcad = by["GCAD"]  # capital gains distribution: cash per share, long + short term
    assert gcad.kind == "dividend" and gcad.amount == D("0.45855") + D("0.11019")
    # Everything else is surfaced, never dropped.
    kinds = {n["type"]: n for n in notes}
    assert set(kinds) == {"cash_merger", "name_change", "redemption", "rights_distribution", "spin_off",
                          "stock_and_cash_merger", "stock_dividend", "stock_merger", "unit_split", "worthless_removal"}
    assert kinds["name_change"]["symbol"] == "BSAQ" and "VFS" in kinds["name_change"]["message"]
    assert kinds["cash_merger"]["symbol"] == "GLOP" and kinds["cash_merger"]["date"] == "2023-07-17"
    assert "were not changed" in kinds["stock_dividend"]["message"]


def test_decimal_split_rates_and_odd_records():
    acts, notes = to_lifecycle([
        {"type": "forward_split", "id": "a", "symbol": "X", "ex_date": "2026-01-05", "new_rate": D("1.5"), "old_rate": 1},
        {"type": "forward_split", "id": "b", "symbol": "Y", "ex_date": "2026-01-05", "new_rate": 1, "old_rate": 1},
        {"type": "cash_dividend", "id": "c", "symbol": "Z", "ex_date": "2026-01-05", "rate": D("0.5"),
         "payable_date": "2026-01-02", "currency": "USD"},
        {"type": "cash_dividend", "id": "d", "symbol": "W", "ex_date": "2026-01-05", "rate": D("0.5"), "currency": "CAD"},
        {"type": "cash_dividend", "id": "e", "symbol": "V", "ex_date": None, "rate": D("0.5")},
    ])
    assert [(a.symbol, a.new, a.old) for a in acts if a.kind == "split"] == [("X", 3, 2)]
    z = next(a for a in acts if a.symbol == "Z")
    assert z.pay_date == z.ex_date  # a pay date before the ex-date is moved to the ex-date
    assert [n["symbol"] for n in notes] == ["Y", "W"] and "CAD" in notes[1]["message"]


# ---- end to end through the adapter --------------------------------------------------------------
def ca_body(day_records: dict) -> dict:
    """The documentation examples, edited: the FCF dividend is moved to AAPL, the SRE split to TSLA and
    the GLOP cash merger to AMD, at the dates given."""
    ex = examples()["corporate_actions"]
    out = {}
    if "dividend" in day_records:
        d = ex["cash_dividends"][0]
        d.update(symbol="AAPL", ex_date=day_records["dividend"][0], record_date=day_records["dividend"][0],
                 payable_date=day_records["dividend"][1], process_date=day_records["dividend"][1], rate=D("0.25"))
        d.update(day_records.get("dividend_extra", {}))
        out["cash_dividends"] = [d]
    if "split" in day_records:
        s = ex["forward_splits"][0]
        s.update(symbol="TSLA", ex_date=day_records["split"], process_date=day_records["split"])
        out["forward_splits"] = [s]
    if "merger" in day_records:
        m = ex["cash_mergers"][0]
        m.update(acquiree_symbol="AMD", effective_date=day_records["merger"], process_date=day_records["merger"])
        out["cash_mergers"] = [m]
    return {"corporate_actions": out, "next_page_token": None}


class Live:
    """An AlpacaFeed and an Exchange on one simulated clock (the feed's wall clock is the exchange clock)."""

    def __init__(self, start: float, ca, data_dir=None, **extra):
        self.clock = SimClock(start)
        self.tr = FakeTransport(routes(**{"/v1/corporate-actions": ca, **extra}))
        self.feed = AlpacaFeed("PKTESTKEY", "test-secret", transport=self.tr, sleep=lambda s: None,
                               wall_clock=self.clock.now)
        self.feed.load_assets()
        self.ex = Exchange(self.feed, self.clock, data_dir=data_dir, session_mode="us")
        self.feed.holdings_source = lambda: held_symbols(self.ex)

    def quote(self, sym, bid, ask, last, trade=True):
        t = iso(self.clock.now())
        msgs = [{"T": "q", "S": sym, "bp": D(bid), "bs": 10, "ap": D(ask), "as": 10, "t": t}]
        if trade:
            msgs.append({"T": "t", "S": sym, "p": D(last), "s": 100, "t": t})
        self.feed._on_stream(msgs)

    def ca_requests(self):
        return [r for r in self.tr.requests if r["path"] == "/v1/corporate-actions"]

    def goto(self, t: float, poll: bool = True):
        self.clock.set(t)
        if poll:
            self.feed.actions.poll()
        return self.ex.tick()


def setup_holdings(lv: Live):
    a = lv.ex.create_account("Margin", "margin")
    for sym, px in (("AAPL", "100.00"), ("TSLA", "200.00"), ("AMD", "50.00")):
        lv.quote(sym, D(px) - D("0.01"), px, px)
    lv.ex.tick()
    for sym, qty in (("AAPL", 100), ("TSLA", 30), ("AMD", 10)):
        (o,) = lv.ex.submit(a.id, {"symbol": sym, "side": "buy", "qty": qty})
        assert o.status == "filled", o.reason
    return a


def test_dividend_split_and_unsupported_notice_applied_end_to_end():
    body = ca_body({"dividend": ("2024-04-24", "2024-04-26"), "split": "2024-04-25", "merger": "2024-04-24"})
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(body, default=str))
    a = setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"))  # day 0: the lifecycle starts here
    lv.goto(ny("2024-04-23", "09:35"))
    req = lv.ca_requests()[-1]["params"]
    assert set(req["symbols"].split(",")) == {"AAPL", "AMD", "TSLA"}  # held symbols only, one batch
    assert lv.ca_requests()[-1]["host"] == "data.alpaca.markets"
    for day in ("2024-04-24", "2024-04-25", "2024-04-26"):
        lv.goto(ny(day, "09:35"))
    assert lv.ex.lifecycle.last_date == date(2024, 4, 26)
    acct = lv.ex.account(a.id)
    assert acct.position("TSLA") == 60  # 2-for-1
    divs = [t for t in acct.ledger.transactions if t.kind == "dividend"]
    assert len(divs) == 1 and sum(v for k, v in divs[0].postings if k == "cash") == D("25.00")
    rows = {r["symbol"]: r for r in lv.ex.positions(a.id)}
    assert rows["AMD"]["notices"][0]["type"] == "cash_merger" and "AMD" in rows["AMD"]["notices"][0]["message"]
    assert any(e[1] == "corporate_action_unsupported" for e in acct.life.log)
    # the feed has reflected the split once and remembers it across a restart
    assert lv.feed.snapshot()["applied_actions"] == ["alpaca:11cfd108-292e-4cc6-bfbf-5999cdbc4029",
                                                     "alpaca:189bd849-ab9f-4b4d-aaaa-a6d415fd976d"]
    assert list(lv.feed.snapshot()["splits"]) == ["TSLA"]


def test_tick_waits_while_the_provider_is_late_or_failing_and_never_skips_or_doubles_a_day():
    body = json.dumps(ca_body({"dividend": ("2024-04-24", "2024-04-24")}), default=str)
    calls = {"n": 0}

    def ca(req):
        calls["n"] += 1
        return (503, "unavailable") if calls["n"] <= 6 else (200, body)  # two failed polls (3 attempts each)

    lv = Live(ny("2024-04-23", "10:00"), ca)
    a = lv.ex.create_account("Margin", "margin", D("10000"))
    lv.quote("AAPL", "99.99", "100.00", "100.00")
    lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 150})  # 15,000 > cash: a margin debit
    assert o.status == "filled"
    lv.goto(ny("2024-04-23", "10:01"), poll=False)
    debit = -lv.ex.account(a.id).cash
    # Midnight passes: the data is not there yet. The day roll waits; nothing of the day is done.
    events = []
    for t in (ny("2024-04-24", "00:00"), ny("2024-04-24", "00:00") + 10):
        lv.goto(t, poll=False)
        events += lv.ex.drain_events()
    assert lv.ex.lifecycle.last_date == date(2024, 4, 23) and lv.ex.lifecycle.waiting["day"] == "2024-04-24"
    assert [e["kind"] for e in events if e.get("kind") == "corporate_actions_wait"] == ["corporate_actions_wait"]
    assert lv.ex.account(a.id).life.accrued_interest == 0
    with pytest.raises(ActionsPending):
        lv.feed.corporate_actions(date(2024, 4, 24), date(2024, 4, 24))
    # The background poll fails twice (with backoff), the tick keeps waiting.
    lv.goto(ny("2024-04-24", "00:01"))
    assert lv.feed.actions.error and lv.ex.lifecycle.last_date == date(2024, 4, 23)
    lv.goto(ny("2024-04-24", "00:01") + 5)  # inside the 30 s backoff: no request
    assert calls["n"] == 3
    lv.goto(ny("2024-04-24", "00:02"))  # second failure, backoff 60 s
    assert calls["n"] == 6 and "503" in lv.ex.lifecycle.waiting["reason"]
    lv.goto(ny("2024-04-24", "00:04"))  # the answer arrives; the day is processed exactly once
    assert lv.ex.lifecycle.last_date == date(2024, 4, 24) and lv.ex.lifecycle.waiting is None
    acct = lv.ex.account(a.id)
    one_day = (debit * D(acct.settings["margin_interest_rate"]) / 100 / 360).quantize(D("0.0000000001"))
    assert debit > 0 and acct.life.accrued_interest == one_day  # one day accrued, not two or none
    assert [t.kind for t in acct.ledger.transactions].count("dividend") == 1
    lv.goto(ny("2024-04-24", "00:05"))
    assert [t.kind for t in lv.ex.account(a.id).ledger.transactions].count("dividend") == 1


def test_action_published_after_its_ex_date_is_applied_late_and_logged():
    state = {"body": json.dumps(ca_body({}), default=str)}
    lv = Live(ny("2024-04-22", "10:00"), lambda req: (200, state["body"]))
    a = setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))  # ex-date passes with nothing published
    assert lv.ex.account(a.id).life.pending_dividends == []
    state["body"] = json.dumps(ca_body({"dividend": ("2024-04-24", "2024-04-30")}), default=str)
    lv.goto(ny("2024-04-25", "09:35"))  # a new day: fetched again, the record is there now
    events = lv.ex.drain_events()
    pend = lv.ex.account(a.id).life.pending_dividends
    assert len(pend) == 1 and pend[0]["amount"] == "25.00"
    assert any(e.get("kind") == "corporate_action_late" for e in events)


def test_restart_and_amended_or_reissued_records_never_apply_twice(tmp_path):
    body = ca_body({"dividend": ("2024-04-24", "2024-04-24"), "split": "2024-04-24"})
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(body, default=str), data_dir=str(tmp_path))
    a = setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-24", "09:35"))
    assert lv.ex.account(a.id).position("TSLA") == 60
    lv.ex.save(force=True)
    # Restart. Alpaca now returns the dividend amended (same id, new rate) and the split re-issued
    # under a new id. Neither is applied again.
    amended = ca_body({"dividend": ("2024-04-24", "2024-04-24"), "split": "2024-04-24",
                       "dividend_extra": {"rate": D("0.30")}})
    amended["corporate_actions"]["forward_splits"][0]["id"] = "new-id-for-the-same-split"
    lv2 = Live(ny("2024-04-25", "09:35"), json.dumps(amended, default=str), data_dir=str(tmp_path))
    assert lv2.ex.load()
    assert lv2.feed.applied_actions == lv.feed.applied_actions  # market.json carried the feed's state
    lv2.goto(ny("2024-04-25", "09:36"))
    lv2.goto(ny("2024-04-26", "09:35"))
    acct = lv2.ex.account(a.id)
    assert acct.position("TSLA") == 60
    assert [t.kind for t in acct.ledger.transactions].count("dividend") == 1
    assert [t.kind for t in acct.ledger.transactions].count("split") == 1
    events = lv2.ex.drain_events()
    amend = [e for e in events if e.get("kind") == "corporate_action_amended"]
    assert len(amend) == 1 and "0.3" in amend[0]["message"] and "nothing was applied twice" in amend[0]["message"]


def test_plan_without_corporate_actions_rolls_on_and_says_so():
    lv = Live(ny("2024-04-22", "10:00"), (403, {"message": "forbidden"}))
    setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"))
    lv.goto(ny("2024-04-23", "09:35"))
    with pytest.raises(ActionsUnavailable):
        lv.feed.corporate_actions(date(2024, 4, 23), date(2024, 4, 23))
    assert lv.ex.lifecycle.last_date == date(2024, 4, 23)
    events = lv.ex.drain_events()
    assert [e for e in events if e.get("kind") == "corporate_actions_unavailable"]
    assert lv.feed.status_info()["corporate_actions"]["state"] == "error"


def test_corporate_action_fetch_is_cached_per_day_and_rate_limited():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str))
    setup_holdings(lv)
    lv.feed.actions.poll()
    n = len(lv.ca_requests())
    lv.feed.actions.poll()
    lv.clock.advance(3600)
    lv.feed.actions.poll()
    assert len(lv.ca_requests()) == n == 1  # once per day for the same symbols
    lv.clock.set(ny("2024-04-23", "00:00") + 1)
    lv.feed.actions.poll()
    assert len(lv.ca_requests()) == 2
    p = lv.ca_requests()[-1]["params"]
    assert p["start"] <= "2024-04-13" and p["end"] >= "2024-08-21"  # widened: Alpaca filters on process date
    calls = []
    lv.feed.rest.http.limiter.acquire = lambda max_wait=60: calls.append(max_wait)
    lv.clock.set(ny("2024-04-24", "00:00") + 1)
    lv.feed.actions.poll()
    assert calls == [30]  # through the shared limiter


def test_tick_never_calls_the_network_for_corporate_actions():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str))
    setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"), poll=False)
    lv.goto(ny("2024-04-23", "09:35"), poll=False)
    lv.goto(ny("2024-04-23", "09:36"), poll=False)
    assert lv.ca_requests() == [] and lv.ex.lifecycle.last_date == date(2024, 4, 22)


def test_upcoming_route_shows_what_is_known_while_loading():
    book = ActionBook(None, lambda: ny("2024-04-22", "10:00"), lambda: ["AAPL"])
    with pytest.raises(ActionsPending) as e:
        book.actions_for(date(2024, 4, 1), date(2024, 5, 1))
    assert e.value.known == [] and "AAPL" in e.value.reason
    feed = SimFeed()
    assert feed.corporate_actions(date(2024, 1, 1), date(2024, 2, 1)) == []  # the scripted feed is unchanged


def test_split_keeps_previous_close_and_charts_consistent():
    body = ca_body({"split": "2024-04-24"})
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(body, default=str))
    setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"))
    lv.feed.bars("TSLA", "1d", 10)
    assert ("TSLA", "1d") in lv.feed._bar_cache
    assert lv.tr.requests[-1]["params"]["adjustment"] == "split"
    lv.goto(ny("2024-04-24", "00:01"))
    assert ("TSLA", "1d") not in lv.feed._bar_cache  # cached bars dropped; refetched adjusted
    q = lv.feed.quote("TSLA")
    assert q.last == D("100.00") and q.ask == D("100.00")  # current quote shown on the new basis
    # A snapshot whose previous daily bar is the raw pre-split close is put on the adjusted basis,
    # because today's trades are near the adjusted price; an already adjusted one is left alone.
    for prev, expect in ((D("200.00"), D("100.00")), (D("101.00"), D("101.00"))):
        snap = {"latestTrade": {"p": D("99.50"), "s": 5, "t": iso(ny("2024-04-24", "09:31"))},
                "latestQuote": {"bp": D("99.49"), "bs": 1, "ap": D("99.51"), "as": 1, "t": iso(ny("2024-04-24", "09:31"))},
                "dailyBar": {"o": D("99"), "h": D("100"), "l": D("98"), "c": D("99.5"), "v": 10, "t": "2024-04-24T04:00:00Z"},
                "prevDailyBar": {"o": prev, "h": prev, "l": prev, "c": prev, "v": 10, "t": "2024-04-23T04:00:00Z"}}
        lv.clock.set(ny("2024-04-24", "09:32"))
        lv.feed.apply_snapshots({"TSLA": snap})
        lv.feed.step(lv.clock.now())
        assert lv.feed.quote("TSLA").prev_close == expect


# ---- real option listings ---------------------------------------------------------------------------
def listing_body():
    """The get-options-contracts example row, repeated for real AAPL contracts: a Wednesday expiry the
    model calendar does not have, three strikes, plus an adjusted contract (root AAPL1, 150 shares)."""
    base = fixture("alpaca/option_contracts.json")["option_contracts"][0]
    rows = []
    for exp in ("2024-04-24", "2024-04-26"):
        for k in ("160", "162.5", "165"):
            for kind in ("call", "put"):
                r = dict(base)
                sym = f"AAPL{exp[2:4]}{exp[5:7]}{exp[8:10]}{'C' if kind == 'call' else 'P'}{int(D(k) * 1000):08d}"
                r.update(symbol=sym, expiration_date=exp, strike_price=k, type=kind, root_symbol="AAPL",
                         underlying_symbol="AAPL", open_interest="10")
                rows.append(r)
    adj = dict(base)
    adj.update(symbol="AAPL240426C00170000", expiration_date="2024-04-26", strike_price="170", root_symbol="AAPL1",
               size="150", underlying_symbol="AAPL")
    rows.append(adj)
    return json.dumps({"option_contracts": rows, "next_page_token": None})


def option_live():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str),
              **{"/v2/options/contracts": listing_body()})
    lv.quote("AAPL", "161.99", "162.00", "162.00")
    lv.ex.tick()
    return lv, lv.ex.create_account("Margin", "margin")


def test_provider_listing_drives_order_validation():
    lv, a = option_live()
    listing = lv.feed.option_listing("AAPL")
    assert sorted(listing) == [date(2024, 4, 24), date(2024, 4, 26)]
    assert listing[date(2024, 4, 26)] == {D("160"), D("162.5"), D("165")}  # AAPL1 adjusted contract left out
    from app.options.chain import expirations
    assert date(2024, 4, 24) not in expirations(date(2024, 4, 22))  # outside the model calendar ...
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL240424C00165000", "side": "buy", "qty": 1})  # ... but tradable
    assert o.status == "filled"
    with pytest.raises(OrderRejected, match="strike is not listed: Alpaca lists no 167.5 strike"):
        lv.ex.preview(a.id, {"symbol": "AAPL240426C00167500", "side": "buy", "qty": 1})
    with pytest.raises(OrderRejected, match="expiry is not listed: Alpaca lists no AAPL options expiring 2024-05-03"):
        lv.ex.preview(a.id, {"symbol": "AAPL240503C00165000", "side": "buy", "qty": 1})
    with pytest.raises(OrderRejected, match="strike is not listed"):
        lv.ex.preview(a.id, {"symbol": "AAPL240426C00170000", "side": "buy", "qty": 1})  # the adjusted contract
    # multi-leg: both legs must be listed
    spread = {"legs": [{"symbol": "AAPL240424C00160000", "side": "buy"}, {"symbol": "AAPL240424C00165000", "side": "sell"}],
              "qty": 1, "type": "limit", "limit_price": "2.00", "price_effect": "debit"}
    assert lv.ex.preview(a.id, spread)["underlying"] == "AAPL"
    bad = copy.deepcopy(spread)
    bad["legs"][1]["symbol"] = "AAPL240424C00167500"
    with pytest.raises(OrderRejected, match="strike is not listed"):
        lv.ex.preview(a.id, bad)


def test_expirations_and_chain_routes_use_listed_contracts():
    lv, a = option_live()
    c = TestClient(create_app(lv.ex, run_loop=False))
    exps = c.get("/api/options/expirations/AAPL").json()
    assert [e["date"] for e in exps] == ["2024-04-24", "2024-04-26"]
    ch = c.get("/api/options/chain/AAPL", params={"expiry": "2024-04-26"}).json()
    assert [r["strike"] for r in ch["rows"]] == [160, 162.5, 165]  # every listed strike, not only quoted ones
    assert ch["source"] == "alpaca-indicative" and ch["model_legs"] == 5  # only the 162.5 call is quoted
    assert ch["rows"][0]["call"]["source"] == "model"
    assert c.get("/api/options/chain/AAPL", params={"expiry": "2024-05-03"}).status_code == 404


def test_chain_with_listing_but_no_option_quotes_is_labelled_model():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str),
              **{"/v2/options/contracts": listing_body(), "/v1beta1/options/snapshots/AAPL": (500, "down")})
    lv.quote("AAPL", "161.99", "162.00", "162.00")
    lv.ex.tick()
    ch = lv.feed.option_chain("AAPL", date(2024, 4, 24), lv.clock.now())
    assert ch["source"] == "model" and ch["simulated"] is True and "not market data" in ch["source_label"]
    assert len(ch["rows"]) == 3


def test_expiry_on_a_provider_listed_wednesday_exercises_on_that_date():
    lv, a = option_live()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL240424C00160000", "side": "buy", "qty": 1})
    assert o.status == "filled"
    lv.goto(ny("2024-04-24", "15:59"))
    assert lv.ex.account(a.id).position("AAPL240424C00160000") == 1
    lv.goto(cal.close_ts(date(2024, 4, 24)))  # the real expiry: Wednesday's close
    acct = lv.ex.account(a.id)
    assert acct.position("AAPL240424C00160000") == 0 and acct.position("AAPL") == 100
    assert [f.kind for f in acct.fills][-2:] == ["exercise", "exercise"] and acct.fills[-1].price == D("160")


def test_held_contract_the_provider_no_longer_lists_is_valued_by_the_model_and_can_be_closed():
    lv, a = option_live()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL240426C00165000", "side": "buy", "qty": 1})
    assert o.status == "filled"
    # Alpaca stops listing that strike (delisted or adjusted); the cached listing is refreshed.
    lv.feed._exp_cache["AAPL"][1][date(2024, 4, 26)].discard(D("165"))
    rows = {r["symbol"]: r for r in lv.ex.positions(a.id)}
    r = rows["AAPL240426C00165000"]
    assert r["price_source"] == "model" and "no longer lists" in r["price_note"] and r["mark"] > 0
    (c,) = lv.ex.submit(a.id, {"symbol": "AAPL240426C00165000", "side": "sell", "qty": 1})
    assert c.status == "filled"


def test_synthetic_feed_keeps_the_model_calendar():
    from app.marketdata.synthetic import SyntheticFeed
    feed = SyntheticFeed(now=ny("2024-04-22", "10:00"))
    ex = Exchange(feed, SimClock(ny("2024-04-22", "10:00")))
    a = ex.create_account("Margin", "margin")
    assert feed.option_listing("NVLX") is None
    with pytest.raises(OrderRejected, match="^expiry is not listed$"):
        ex.preview(a.id, {"symbol": "NVLX240424C00180000", "side": "buy", "qty": 1})


# ---- missing prices -----------------------------------------------------------------------------------
def test_missing_side_is_filled_only_from_a_recent_trade():
    st = SymbolState("X")
    st.apply_trade({"p": D("10.00"), "s": 5}, 100.0, count_volume=True)
    st.apply_quote({"bp": D("9.99"), "bs": 1, "ap": D("0"), "as": 0}, 101.0)
    q = st.to_quote(now=120.0)
    assert (q.bid, q.ask) == (D("9.99"), D("10.00"))  # trade 20 s old: the ask is the last trade
    q = st.to_quote(now=100.0 + al.RECENT_TRADE + 1)
    assert (q.bid, q.ask, q.last) == (D("9.99"), D("0"), D("10.00"))  # too old: the ask stays empty
    assert q.ask_size == 0


def test_no_trade_yet_falls_back_to_previous_close_labelled_stale():
    lv = Live(ny("2024-04-22", "07:00"), json.dumps(ca_body({}), default=str))
    snap = {"prevDailyBar": {"o": D("170"), "h": D("171"), "l": D("169"), "c": D("170.50"), "v": 1,
                             "t": "2024-04-19T04:00:00Z"}}
    lv.feed.apply_snapshots({"AAPL": snap})
    lv.ex.tick()
    q = lv.ex.quote("AAPL")
    assert (q.bid, q.ask, q.last) == (D("0"), D("0"), D("170.50"))
    assert lv.ex.mark("AAPL") == D("170.50")
    note = lv.feed.quote_note("AAPL")
    assert note["stale"] and "previous close" in note["note"]
    c = TestClient(create_app(lv.ex, run_loop=False))
    row = c.get("/api/quotes", params={"symbols": "AAPL"}).json()["AAPL"]
    assert row["stale"] is True and "previous close" in row["note"]


def test_market_orders_on_a_one_sided_or_empty_book_reject_with_the_cause_or_wait():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str))
    a = lv.ex.create_account("Margin", "margin")
    old = iso(ny("2024-04-22", "09:31"))
    lv.feed._on_stream([{"T": "t", "S": "AMD", "p": D("50.00"), "s": 100, "t": old},
                        {"T": "q", "S": "AMD", "bp": D("49.90"), "bs": 1, "ap": D("0"), "as": 0,
                         "t": iso(lv.clock.now())}])
    lv.ex.tick()
    with pytest.raises(OrderRejected, match="No ask for AMD right now, so a market buy has no price to fill at"):
        lv.ex.submit(a.id, {"symbol": "AMD", "side": "buy", "qty": 1})
    (lim,) = lv.ex.submit(a.id, {"symbol": "AMD", "side": "buy", "qty": 1, "type": "limit", "limit_price": "50.00"})
    assert lim.status == "working"  # a limit order rests until there is an ask
    (o,) = lv.ex.submit(a.id, {"symbol": "AMD", "side": "sell", "qty": 1, "type": "limit", "limit_price": "49.00"})
    assert o.status == "working"  # conservative: the engine waits for a two-sided book
    # Unknown and not-yet-quoted symbols name the real cause.
    with pytest.raises(OrderRejected, match="unknown symbol ZZZZ"):
        lv.ex.preview(a.id, {"symbol": "ZZZZ", "side": "buy", "qty": 1})
    with pytest.raises(OrderRejected, match="No quote has arrived from Alpaca for TSLA yet"):
        lv.ex.preview(a.id, {"symbol": "TSLA", "side": "buy", "qty": 1})
    # The account summary still works with orders resting on a symbol without a usable quote.
    assert lv.ex.summary(a.id)["prices_unavailable"] == []


def test_resting_market_sell_never_fills_against_an_empty_bid():
    feed = SimFeed()
    ex = Exchange(feed, SimClock(0))
    a = ex.create_account("Margin", "margin")
    feed.set_quote("X", "9.99", "10.00")
    ex.tick()
    ex.submit(a.id, {"symbol": "X", "side": "buy", "qty": 10})
    feed.set_quote("X", "0", "10.00", last="10.00")
    ex.tick()
    ex.session_mode = "us"
    ex.clock.set(ny("2024-04-22", "08:00"))  # closed for market orders: accepted, waits for the open
    (o,) = ex.submit(a.id, {"symbol": "X", "side": "sell", "qty": 10})
    ex.clock.set(ny("2024-04-22", "09:31"))
    feed.set_quote("X", "0", "10.00", last="10.00")
    ex.tick()
    assert ex.engine.orders[o.id].status == "working"  # no fill at a made-up 0.01
    feed.set_quote("X", "9.98", "10.00", last="9.99")
    ex.clock.advance(1)
    ex.tick()
    assert ex.engine.orders[o.id].status == "filled" and ex.engine.orders[o.id].avg_fill_price == D("9.98")


def test_option_marks_and_prices_unavailable_only_when_there_is_no_price():
    lv, a = option_live()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL240426P00160000", "side": "buy", "qty": 1})
    assert o.status == "filled"
    # A provider option quote with no bid or ask but a last trade is marked at the last trade, not 0.
    lv.feed._opt["AAPL240426P00160000"] = (al.Quote(symbol="AAPL240426P00160000", bid=D(0), ask=D(0), bid_size=D(0),
                                                    ask_size=D(0), last=D("1.20")), lv.clock.now())
    assert lv.ex.mark("AAPL240426P00160000") == D("1.20")
    assert lv.ex.summary(a.id)["prices_unavailable"] == []
    lv.feed._opt.clear()
    lv.feed.quotes.pop("AAPL")
    lv.feed._state.pop("AAPL")
    s = lv.ex.summary(a.id)
    assert s["prices_unavailable"] == ["AAPL", "AAPL240426P00160000"] and s["margin_call"] is False
    row = lv.ex._annotate_position({}, "AAPL240426P00160000", lv.ex.mark("AAPL240426P00160000"))
    assert row["price_source"] is None and row["price_note"].startswith("Not valued:")


# ---- symbol lists ---------------------------------------------------------------------------------------
def test_symbol_list_is_bounded_and_search_covers_everything():
    info = {f"S{i:05d}": {"symbol": f"S{i:05d}", "name": f"Company {i}", "sector": ""} for i in range(11000)}
    info["AAPL"] = {"symbol": "AAPL", "name": "Apple Inc", "sector": ""}
    info["AAP"] = {"symbol": "AAP", "name": "Advance Auto Parts", "sector": ""}
    info["XAAP"] = {"symbol": "XAAP", "name": "Example", "sector": ""}
    feed = SimFeed(info)
    ex = Exchange(feed, SimClock(0))
    ex.create_account("Margin", "margin")
    ex.watchlists = {"Main": ["S10999", "AAPL", "MKTX"]}  # MKTX: a fictional symbol the live feed does not know
    c = TestClient(create_app(ex, run_loop=False))
    rows = c.get("/api/symbols").json()
    assert len(rows) == 50 and [r["symbol"] for r in rows[:2]] == ["S10999", "AAPL"]
    assert len(c.get("/api/symbols", params={"limit": 500}).json()) == 500
    assert c.get("/api/symbols", params={"limit": 501}).status_code == 422
    found = c.get("/api/symbols", params={"q": "aap"}).json()
    assert [r["symbol"] for r in found] == ["AAP", "AAPL", "XAAP"]  # exact, then prefix, then contains
    assert c.get("/api/symbols", params={"q": "S10998"}).json()[0]["symbol"] == "S10998"  # deep in the list
    assert c.get("/api/symbols", params={"q": "company 1099"}).json()[0]["symbol"] == "S01099"
    look = c.get("/api/symbols/lookup", params={"symbols": "MKTX,aapl,,S00001"}).json()
    assert [r["symbol"] for r in look] == ["AAPL", "S00001"]


# ---- replay sessions from provider bars ------------------------------------------------------------------
def bars_route(day: str, refuse_sip: bool = False):
    """Serves the stockbars documentation example row, re-timed: 1-minute bars for the first minutes of
    `day`'s session (plus one pre-market bar that must be dropped) and daily bars before it, with a 2-for-1
    split after the day so the split-adjusted daily history is half the raw one."""
    base = fixture("alpaca/stock_bars.json")["bars"]["AAPL"][0]
    open_ts = ny(day, "09:30")

    def route(req):
        p = req["params"]
        if refuse_sip and p["feed"] == "sip":
            return 403, {"message": "subscription does not permit querying recent SIP data"}
        sym = p["symbols"]
        rows = []
        if p["timeframe"] == "1Min":
            assert p["adjustment"] == "raw" and p["sort"] == "asc"
            for i, t in enumerate([open_ts - 3600] + [open_ts + 60 * k for k in range(5)]):
                r = dict(base)
                px = D("400") + i
                r.update(t=iso(t), o=px, h=px + D("0.5"), l=px - D("0.5"), c=px + D("0.25"), v=1000 + i)
                rows.append(r)
        else:
            scale = D("0.5") if p["adjustment"] == "split" else D(1)
            d = date.fromisoformat(day) - timedelta(days=1)
            for k in range(30):
                while not cal.is_trading_day(d):
                    d -= timedelta(days=1)
                r = dict(base)
                px = (D("390") + k) * scale
                r.update(t=f"{d.isoformat()}T04:00:00Z", o=px, h=px, l=px, c=px, v=5000)
                rows.append(r)
                d -= timedelta(days=1)
            rows.reverse()
            if p["start"] > (date.fromisoformat(day) - timedelta(days=20)).isoformat():
                rows = rows[-5:]
        return 200, {"bars": {sym: rows}, "next_page_token": None, "symbol": sym}
    return route


def test_replay_session_built_from_provider_bars(tmp_path):
    tr = FakeTransport({"/v2/stocks/bars": bars_route("2024-04-22")})
    rest = al.AlpacaREST("PKTESTKEY", "test-secret", tr, sleep=lambda s: None)
    st = build_from_provider(rest, date(2024, 4, 22), ["SPY", "AAPL"], tmp_path / "x.pdsession.gz",
                             names={"SPY": "SPDR S&P 500 ETF Trust"})
    s = read_session(st["path"])
    h = s.header
    assert h["simulated"] is False and h["built_from"] == "1-minute bars" and h["date"] == "2024-04-22"
    assert h["source"].startswith("Alpaca historical 1-minute bars, SIP feed; prices as traded on 2024-04-22")
    assert "not tick data" in h["description"] and "approximated" in h["description"]
    assert s.start == ny("2024-04-22", "09:30") and s.end == cal.close_ts(date(2024, 4, 22))
    assert [x["name"] for x in h["symbols"]] == ["AAPL", "SPDR S&P 500 ETF Trust"]
    assert h["initial"]["SPY"]["open"] == "401.00"  # the pre-market bar was dropped
    trades = [e for e in s.events if e[1] == "T" and e[2] == "SPY"]
    assert len(trades) == 20 and min(e[0] for e in s.events) >= s.start
    # daily context: split-adjusted history rescaled to the day's raw basis (no jump at the split)
    assert s.daily["SPY"][-1][4] == pytest.approx(390.0) and len(s.daily["SPY"]) == 30
    assert all(r["params"]["feed"] == "sip" for r in tr.requests)


def test_replay_build_falls_back_to_iex_and_the_cli_reports_it(tmp_path, capsys):
    from app.marketdata.replay.__main__ import cmd_fetch, main
    tr = FakeTransport({"/v2/stocks/bars": bars_route("2024-04-22", refuse_sip=True)})
    rest = al.AlpacaREST("PKTESTKEY", "test-secret", tr, sleep=lambda s: None)

    class A:
        date, symbols, out, feed, label, spread_bps = "2024-04-22", "SPY,NODATA", str(tmp_path / "d"), "sip", "", 2.0
    orig = bars_route("2024-04-22", refuse_sip=True)
    tr.routes["/v2/stocks/bars"] = lambda req: (200, {"bars": {}, "next_page_token": None}) \
        if req["params"]["symbols"] == "NODATA" and req["params"]["feed"] == "iex" else orig(req)
    assert cmd_fetch(A, rest=rest) == 0
    out = capsys.readouterr().out
    assert "SPY IEX" in out and "No bars that day for: NODATA" in out and "PKTESTKEY" not in out
    s = read_session(str(tmp_path / "d.pdsession.gz"))
    assert "IEX feed" in s.header["source"] and "IEX exchange's trades" in s.header["description"]
    A.date = "2024-04-20"  # a Saturday
    assert cmd_fetch(A, rest=rest) == 1 and "not a US trading day" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["fetch", "--date", "2024-04-22"])  # --symbols and --out are required


# ---- the manual check script ---------------------------------------------------------------------------
def test_check_script_reports_the_new_items_and_never_prints_secrets(tmp_path):
    import time as _time
    snaps = fixture("alpaca/stock_snapshots.json")

    def snapshots(req):
        syms = req["params"]["symbols"].split(",")
        return 200, json.dumps({s: snaps["AAPL"] for s in syms}, default=str)

    day = cal.ny_date(_time.time()) - timedelta(days=1)
    while not cal.is_trading_day(day):
        day -= timedelta(days=1)
    spy_listing = listing_body().replace("AAPL", "SPY")
    chain = raw("alpaca/option_chain.json").replace("AAPL", "SPY")
    tr = FakeTransport(routes(**{"/v2/stocks/snapshots": snapshots, "/v2/options/contracts": spy_listing,
                                 "/v1beta1/options/snapshots/SPY": chain,
                                 "/v1/corporate-actions": raw(EXAMPLES)}))
    bars = bars_route(day.isoformat())
    tr.routes["/v2/stocks/bars"] = lambda req: bars(req) if req["params"].get("sort") == "asc" \
        else (200, raw("alpaca/stock_bars.json").replace("AAPL", "SPY"))
    from test_alpaca import S

    class Conn:
        frames = [S["connected"], S["authenticated"], S["subscription_aapl_amd"]]

        def __init__(self, url):
            self.frames = list(Conn.frames)

        def send(self, text):
            pass

        def recv(self, timeout):
            if not self.frames:
                raise ConnectionError("closed")
            return json.dumps(self.frames.pop(0), default=str)

        def close(self):
            pass

    env = {"APCA_API_KEY_ID": "PKSECRETKEYID", "APCA_API_SECRET_KEY": "very-secret-value"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = al._check(seconds=0.5, thin="AMD", transport=tr, connector=Conn, environ=env, out_dir=str(tmp_path))
    out = buf.getvalue()
    lines = {l.split(":")[0]: l for l in out.splitlines()}
    for name in ("PASS option contracts", "PASS corporate actions", "PASS thin symbol snapshot",
                 "PASS replay session from bars"):
        assert name in lines, out
    assert "PKSECRETKEYID" not in out and "very-secret-value" not in out
    assert "expiries not on a Friday" in lines["PASS option contracts"]
    assert "shown as notices" in lines["PASS corporate actions"]
    # a failure says FAIL with the cause and still hides the keys
    tr.routes["/v1/corporate-actions"] = (403, {"message": "forbidden for PKSECRETKEYID"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = al._check(seconds=0.5, thin="AMD", transport=tr, connector=Conn, environ=env, out_dir=str(tmp_path))
    out = buf.getvalue()
    assert code == 1 and "FAIL corporate actions: Alpaca returned HTTP 403" in out and "PKSECRETKEYID" not in out


# ---- properties -------------------------------------------------------------------------------------------
from hypothesis import given, settings as hsettings, strategies as st  # noqa: E402

from app.lifecycle.actions import dividend as dividend_action, split as split_action  # noqa: E402


class FlakyFeed(SimFeed):
    """A scripted feed that is not ready on some calls, like a live provider answering late or failing."""
    late_actions = True

    def __init__(self, ready):
        super().__init__()
        self.ready = list(ready)

    def corporate_actions(self, start, end):
        if self.ready and not self.ready.pop(0):
            raise ActionsPending("not loaded yet")
        return super().corporate_actions(start, end)


@hsettings(max_examples=40, deadline=None)
@given(ready=st.lists(st.booleans(), max_size=60), hours=st.lists(st.integers(1, 30), min_size=5, max_size=25),
       restart_at=st.integers(0, 25))
def test_waiting_for_the_feed_never_skips_or_repeats_a_day(ready, hours, restart_at, tmp_path_factory):
    """Whatever the feed's availability and the clock's jumps (and a restart anywhere), every day's accrual
    happens once, the dividend is paid once and the split applied once, once the feed answers."""
    d = tmp_path_factory.mktemp("st")
    start = datetime(2026, 3, 2, 15, tzinfo=timezone.utc).timestamp()  # a Monday
    feed = FlakyFeed(ready)
    feed.schedule(dividend_action("ABC", date(2026, 3, 4), date(2026, 3, 6), "0.50"))
    feed.schedule(split_action("ABC", date(2026, 3, 5), 2, 1))
    ex = Exchange(feed, SimClock(start), data_dir=str(d))
    a = ex.create_account("Margin", "margin", D("1000"))
    feed.set_quote("ABC", "9.99", "10.00", ts=start)
    ex.tick()
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 200})  # 2,000: a 1,000 debit
    debit = -ex.account(a.id).cash
    t = start
    for i, h in enumerate(hours + [24 * 9]):
        t += h * 3600
        if i == restart_at:
            ex.save(force=True)
            feed2 = FlakyFeed(feed.ready)
            feed2.actions, feed2.quotes, feed2.info = feed.actions, feed.quotes, feed.info
            feed2.applied_actions = set(feed.applied_actions)
            feed = feed2
            ex = Exchange(feed, SimClock(t), data_dir=str(d))
            assert ex.load()
        ex.clock.set(t)
        ex.tick()
    feed.ready = []
    for _ in range(3):
        ex.clock.advance(60)
        ex.tick()
    acct = ex.account(a.id)
    days = (ex.lifecycle.last_date - date(2026, 3, 2)).days
    assert ex.lifecycle.last_date == ex.lifecycle.days.today(ex.now())
    rate = D(acct.settings["margin_interest_rate"]) / 100 / 360
    posted = sum((v for t_ in acct.ledger.transactions for k, v in t_.postings if k == "expense:margin_interest"), D(0))
    accrued = acct.life.accrued_interest + posted
    # cash moves (dividend paid on the 6th) change the debit after that day; bound it instead of recomputing
    assert (debit - 100) * rate * days - 1 <= accrued <= debit * rate * days + 1
    kinds = [t_.kind for t_ in acct.ledger.transactions]
    assert kinds.count("dividend") == 1 and kinds.count("split") == 1 and acct.position("ABC") == 400


@hsettings(max_examples=60, deadline=None)
@given(bid=st.integers(0, 2000), ask=st.integers(0, 2000), qty=st.integers(1, 500), side=st.sampled_from(["buy", "sell"]))
def test_market_orders_never_fill_against_an_empty_side(bid, ask, qty, side):
    from app.core.models import Order, Quote
    from app.matching.engine import MatchingEngine
    e = MatchingEngine()
    q = Quote(symbol="X", bid=D(bid) / 100, ask=D(ask) / 100, bid_size=D(100), ask_size=D(100), last=D("10"))
    ev = e.submit(Order(id="O1", account_id="A", symbol="X", side=side, qty=D(qty)), q, 0.0)
    fills = [o for k, o in ev if k == "fill"]
    top = q.ask if side == "buy" else q.bid
    if top <= 0 or q.ask <= 0:
        assert fills == []
    else:
        assert fills and (fills[0].price >= q.ask if side == "buy" else fills[0].price <= q.bid)


# ---- quote age (stale) ---------------------------------------------------------------------------------
def test_stale_threshold_per_session_state():
    mon = "2024-04-22"
    assert not al.is_stale(ny(mon, "10:00") - 100, ny(mon, "10:00"))  # regular: up to 120 s
    assert al.is_stale(ny(mon, "10:00") - 121, ny(mon, "10:00"))
    assert not al.is_stale(ny(mon, "08:00") - 800, ny(mon, "08:00"))  # pre-market: up to 15 minutes
    assert al.is_stale(ny(mon, "08:00") - 901, ny(mon, "08:00"))
    assert not al.is_stale(ny(mon, "15:59"), ny("2024-04-23", "02:00"))  # closed: the latest session's data is fine
    assert al.is_stale(ny("2024-04-19", "15:59"), ny("2024-04-23", "02:00"))  # ... older than it is stale
    assert not al.is_stale(ny("2024-04-19", "15:59"), ny("2024-04-21", "12:00"))  # a Sunday: Friday is the latest
    assert al.is_stale(0.0, ny(mon, "10:00"))


def test_stale_quotes_are_flagged_and_never_filled_against():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str))
    a = lv.ex.create_account("Margin", "margin")
    lv.quote("AAPL", "99.99", "100.00", "100.00")
    lv.ex.tick()
    (o,) = lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 10})
    assert o.status == "filled" and lv.ex.quote("AAPL").stale is False
    lv.clock.advance(121)  # nothing new for two minutes in the regular session
    q = lv.ex.quote("AAPL")
    assert q.stale is True and lv.feed.quote_note("AAPL")["stale"] is True
    c = TestClient(create_app(lv.ex, run_loop=False))
    row = c.get("/api/quotes", params={"symbols": "AAPL"}).json()["AAPL"]
    assert row["stale"] is True and "2 minutes old" in row["note"]
    with pytest.raises(OrderRejected, match="The quote for AAPL is stale, so a market order has no current price"):
        lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 1})
    # a marketable limit order rests instead of filling against the old ask
    (lim,) = lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 1, "type": "limit", "limit_price": "101.00"})
    assert lim.status == "working"
    # a snapshot that only repeats the old quote is still stale and fills nothing on the tick
    lv.feed.apply_snapshots({"AAPL": {"latestQuote": {"bp": D("99.99"), "bs": 1, "ap": D("100.00"), "as": 1,
                                                      "t": iso(ny("2024-04-22", "10:00"))}}})
    lv.feed._changed.add("AAPL")
    lv.ex.tick()
    assert lv.ex.engine.orders[lim.id].status == "working"
    s = lv.ex.summary(a.id)
    assert s["stale_prices"] == ["AAPL"] and s["prices_unavailable"] == [] and s["equity"] > 0  # still valued
    assert lv.ex.positions(a.id)[0]["stale"] is True
    lv.quote("AAPL", "100.49", "100.50", "100.50")  # a new quote arrives
    lv.ex.tick()
    assert lv.ex.engine.orders[lim.id].status == "filled" and lv.ex.engine.orders[lim.id].avg_fill_price == D("100.50")
    assert lv.ex.summary(a.id)["stale_prices"] == []


def test_liquidation_skips_stale_quotes():
    lv = Live(ny("2024-04-22", "10:00"), json.dumps(ca_body({}), default=str))
    a = lv.ex.create_account("Margin", "margin", D("5000"))
    lv.quote("AAPL", "99.99", "100.00", "100.00")
    lv.ex.tick()
    lv.ex.submit(a.id, {"symbol": "AAPL", "side": "buy", "qty": 100})
    lv.quote("AAPL", "59.99", "60.00", "60.00")
    lv.ex.tick()
    lv.clock.advance(300)
    assert lv.ex.quote("AAPL").stale
    assert lv.ex.lifecycle.liquidate(lv.ex.account(a.id), lv.clock.now()) == []
    assert lv.ex.account(a.id).position("AAPL") == 100


# ---- margin checks around corporate actions ---------------------------------------------------------------
class HoldFeed(SimFeed):
    def __init__(self, holds):
        super().__init__()
        self.holds = holds

    def margin_holds(self, today, applied=(), since=None):
        return self.holds


def under_water(holds):
    feed = HoldFeed(holds)
    ex = Exchange(feed, SimClock(ny("2024-04-22", "10:00")), session_mode="us")
    a = ex.create_account("Margin", "margin", D("6000"))
    a.settings.update(margin_call_grace_hours="0")
    feed.set_quote("ABC", "99.99", "100.00", ts=ex.now())
    ex.tick()
    ex.submit(a.id, {"symbol": "ABC", "side": "buy", "qty": 100})
    feed.set_quote("ABC", "39.99", "40.00", ts=ex.now())  # looks like a 60% loss (it is a 5-for-2 split)
    ex.clock.advance(1)
    ex.tick()
    ex.clock.advance(1)
    ex.tick()
    return ex, a


def test_known_unapplied_split_pauses_margin_checks():
    ex, a = under_water({"ABC": [{"kind": "split_pending",
                                  "reason": "the 5-for-2 stock split of ABC effective 2024-04-22 is not applied yet"}]})
    acct = ex.account(a.id)
    assert acct.life.margin_call_since is None and acct.position("ABC") == 100
    s = ex.summary(a.id)
    assert s["margin_call"] is False and "5-for-2" in s["margin_paused"][0]
    assert any(e.get("kind") == "margin_paused" for e in ex.drain_events())
    ex.feed.holds = {}
    ex.clock.advance(1)
    ex.tick()
    assert ex.account(a.id).life.margin_call_since is not None  # once applied, the normal rules apply


def test_unprocessable_action_pauses_liquidation_with_a_notice():
    ex, a = under_water({"ABC": [{"kind": "unsupported",
                                  "reason": "ABC had a stock merger on 2024-04-22 that PaperDesk cannot apply"}]})
    acct = ex.account(a.id)
    assert acct.life.margin_call_since is not None  # the call is shown ...
    assert acct.position("ABC") == 100 and acct.life.liquidation_paused  # ... but nothing is sold
    assert any("liquidation paused: ABC had a stock merger" in e[2] for e in acct.life.log)
    assert ex.summary(a.id)["margin_call"] is True
    ex2, a2 = under_water({})  # without the hold the same account is liquidated
    assert ex2.account(a2.id).position("ABC") < 100


def test_action_book_holds():
    book = ActionBook(None, lambda: ny("2024-04-24", "10:00"), lambda: ["TSLA", "AMD"])
    acts, notes = to_lifecycle(corporate_action_records(ca_body({"split": "2024-04-24", "merger": "2024-04-23"})
                                                        ["corporate_actions"]))
    book.actions = {x.id: x for x in acts}
    book.notices = {n["id"]: n for n in notes}
    h = book.holds(date(2024, 4, 24), set(), date(2024, 4, 1))
    assert h["TSLA"][0]["kind"] == "split_pending" and h["AMD"][0]["kind"] == "unsupported"
    split_id = acts[0].id
    assert "TSLA" not in book.holds(date(2024, 4, 24), {split_id}, date(2024, 4, 1))
    assert "TSLA" not in book.holds(date(2024, 4, 24), {acts[0].revision_key}, date(2024, 4, 1))  # re-issued
    assert "TSLA" not in book.holds(date(2024, 4, 24), set(), date(2024, 4, 24))  # before the accounts started
    assert "AMD" not in book.holds(date(2024, 5, 10), set(), None)  # the liquidation hold lasts a few days


def test_waiting_day_roll_pauses_margin_checks():
    lv = Live(ny("2024-04-22", "10:00"), (503, "down"))
    a = setup_holdings(lv)
    lv.goto(ny("2024-04-22", "11:00"), poll=False)
    lv.goto(ny("2024-04-23", "00:00") + 5, poll=False)
    assert lv.ex.lifecycle.waiting is not None
    assert "waiting for corporate action data" in lv.ex.summary(a.id)["margin_paused"][0]


# ---- closing prices for expiry settlement ------------------------------------------------------------------
def test_closing_price_is_fetched_off_the_tick_and_cached():
    base = fixture("alpaca/stock_bars.json")["bars"]["AAPL"][0]

    def bars(req):
        p = req["params"]
        assert p["timeframe"] == "1Day" and p["adjustment"] == "raw"
        r = dict(base, t="2024-04-26T04:00:00Z", c=D("169.30"))
        return 200, {"bars": {p["symbols"]: [r] if p["symbols"] == "AAPL" else []}, "next_page_token": None}
    lv = Live(ny("2024-04-26", "15:00"), json.dumps(ca_body({}), default=str), **{"/v2/stocks/bars": bars})
    r = lv.feed.closing_price("AAPL", date(2024, 4, 26))
    assert r.ready is False and r.price is None
    assert lv.feed.fetch_closes() == 0  # the day is not over: nothing is fetched yet
    lv.clock.set(cal.close_ts(date(2024, 4, 26)) + 20 * 60)
    n = len(lv.tr.requests)
    assert lv.feed.closing_price("AAPL", date(2024, 4, 26)).ready is False and len(lv.tr.requests) == n  # no I/O
    assert lv.feed.fetch_closes() == 1
    r = lv.feed.closing_price("AAPL", date(2024, 4, 26))
    assert r.ready and r.price == D("169.30") and "SIP" in r.note
    lv.feed.fetch_closes()
    assert len(lv.tr.requests) == n + 1  # cached for good
    lv.feed.closing_price("AMD", date(2024, 4, 26))
    lv.feed.fetch_closes()
    r = lv.feed.closing_price("AMD", date(2024, 4, 26))
    assert r.ready and r.price is None and "no daily bar" in r.note


def test_closing_price_from_the_simulated_feed():
    from app.marketdata.synthetic import SyntheticFeed
    feed = SyntheticFeed(now=ny("2024-04-22", "10:00"))
    row = feed.store.daily["NVLX"][-1]
    day = date(1970, 1, 1) + timedelta(seconds=row[0])
    r = feed.closing_price("NVLX", day)
    assert r.ready and r.price == D(str(row[4])).quantize(D("0.01"))
    none = feed.closing_price("NVLX", date(1999, 1, 4))
    assert none.ready and none.price is None and "no synthetic daily bar" in none.note
