"""Schema-free fuzzing of every non-GET /api route (tools/soak/fuzz.py), plus minimal reproductions
of the server errors it found. Set PAPERDESK_FUZZ_REQUESTS for a longer run."""
import json
import os
import warnings

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from helpers import D, T0
from tools.soak import invariants as inv
from tools.soak.fuzz import Fuzzer
from tools.soak.sim import state_text

# Server errors already reported, each with an xfail test below: (exception type, app frame prefix).
KNOWN = {("InvalidOperation", "app/core/money.py"), ("RecursionError", "")}


def exchange(tmp_path):
    ex = Exchange(SyntheticFeed(seed=3, now=T0, history_days=10), SimClock(T0), data_dir=str(tmp_path))
    ex.create_account("Margin", "margin")
    ex.create_account("Cash", "cash", D("25000"))
    ex.tick()
    ex.submit("A0001", {"symbol": "NVLX", "side": "buy", "qty": 3})
    ex.submit("A0001", {"symbol": "NVLX", "side": "buy", "qty": 1, "type": "limit", "limit_price": "1", "tif": "gtc"})
    return ex


def known(err: dict) -> bool:
    kind = err["error"].split(":")[0]
    return any(kind == k and err["where"].startswith(w) for k, w in KNOWN)


@pytest.mark.parametrize("seed", [1, 2])
def test_fuzzing_every_write_route_gives_no_new_server_errors_and_keeps_state_sound(tmp_path, seed):
    warnings.simplefilter("ignore")
    ex = exchange(tmp_path)
    fz = Fuzzer(ex, seed)
    assert len(fz.routes) >= 25
    out = fz.run(int(os.environ.get("PAPERDESK_FUZZ_REQUESTS", "250")))
    new = [e for e in out["server_errors"] if not known(e)]
    assert not new, json.dumps(new[:5], indent=1)
    assert out["statuses"].get(200, 0) > 20  # the fuzzer also gets through to the real work
    book = inv.OrderBook()
    for a in ex.accounts.values():
        inv.check_ledger_incremental(a, 0)
        inv.check_ledger_full(a)
        inv.check_lots(a)
        inv.check_cash_account(a)
        inv.check_summary(ex, a)
    inv.check_orders_full(ex, book)
    text = state_text(ex)
    inv.scan_state_json(text)
    ex.save(force=True)
    ex2 = Exchange(SyntheticFeed(seed=3, now=T0, history_days=10), SimClock(ex.now()), data_dir=str(tmp_path))
    assert ex2.load() and state_text(ex2) == text


# ---- minimal reproductions -------------------------------------------------------------------------
def client(tmp_path):
    return TestClient(create_app(exchange(tmp_path), run_loop=False), raise_server_exceptions=False)


@pytest.mark.xfail(strict=True, reason=(
    "core/money.py parse() raises decimal.InvalidOperation (an ArithmeticError, not a ValueError) for text "
    "that is not a number; api/server.py create_account/reset_account and routes_replay.start call "
    "it outside any try, so the server answers 500 instead of 400"))
@pytest.mark.parametrize("method,url,body", [
    ("POST", "/api/accounts", {"name": "x", "starting_cash": "abc"}),
    ("POST", "/api/accounts/A0001/reset", {"starting_cash": "abc"}),
    ("POST", "/api/accounts/A0001/reset", {"starting_cash": [1]}),
    ("POST", "/api/replay/start", {"session": "sample-2026-03-13", "starting_cash": "abc"}),
])
def test_a_non_numeric_amount_is_a_400_not_a_500(tmp_path, method, url, body):
    r = client(tmp_path).request(method, url, json=body)
    assert r.status_code == 400, (r.status_code, r.text)


@pytest.mark.xfail(strict=True, reason=(
    "a deeply nested JSON body (5000 levels) on any route declared `body: dict = Body(...)` fails validation, "
    "and FastAPI's jsonable_encoder recurses through the input to build the 422 answer: RecursionError, 500"))
def test_a_deeply_nested_body_is_refused_with_a_4xx(tmp_path):
    r = client(tmp_path).post("/api/alerts", content=b"[" * 5000 + b"]" * 5000, headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500, r.status_code
