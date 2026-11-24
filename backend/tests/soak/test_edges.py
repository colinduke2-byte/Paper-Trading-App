"""Report routes on the shortest histories: a brand-new account, a reset one, one day of samples."""
import warnings

from fastapi.testclient import TestClient

from app.analytics.common import ny_date
from app.api.server import create_app
from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed
from helpers import D, T0
from tools.soak.api_checks import check_routes


class _Sim:  # what check_routes needs from a Sim
    def __init__(self, ex):
        self.ex, self.seed, self.step_no = ex, 0, 0


def test_reports_answer_for_empty_single_sample_and_reset_histories():
    warnings.simplefilter("ignore")
    ex = Exchange(SyntheticFeed(seed=3, now=T0, history_days=5), SimClock(T0))
    a = ex.create_account("New", "margin")
    c = ex.create_account("Cash", "cash", D("1000"))
    client = TestClient(create_app(ex, run_loop=False), raise_server_exceptions=False)
    check_routes(_Sim(ex), client)  # no tick yet, no equity samples, no fills
    ex.tick()
    check_routes(_Sim(ex), client)  # one sample
    ex.submit(a.id, {"symbol": "NVLX", "side": "buy", "qty": 1})
    ex.submit(a.id, {"symbol": "NVLX", "side": "sell", "qty": 1})
    ex.clock.advance(61)
    ex.tick()
    check_routes(_Sim(ex), client)  # one closed trade, two samples on one day
    ex.reset_account(a.id, D("5000"))
    check_routes(_Sim(ex), client)  # reset: history starts again
    month = ny_date(ex.now()).strftime("%Y-%m")
    for aid in (a.id, c.id):
        an = client.get(f"/api/accounts/{aid}/analytics").json()
        assert an["returns"]["volatility"] is None and an["returns"]["sharpe"] is None
        assert client.get(f"/api/accounts/{aid}/statements/{month}").status_code == 200
        assert client.get(f"/api/accounts/{aid}/performance").json()["win_rate"] is None
