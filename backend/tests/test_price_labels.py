"""Reports and statements label their prices from the active feed, as the top banner does."""
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.marketdata.labels import default_benchmark, price_source
from tests.helpers import make_exchange, move


class LiveLike:
    """Stands in for the Alpaca feed's labels (no network)."""
    name, simulated = "alpaca", False

    def status_info(self):
        return {"data_source": "Alpaca", "data_exchange": "IEX"}


def test_price_source_words():
    sim = price_source(type("F", (), {"name": "synthetic", "simulated": True, "status_info": lambda s: {}})())
    assert sim == {"simulated": True, "price_source": "simulated prices", "price_note": "All figures use simulated prices."}
    live = price_source(LiveLike())
    assert live["simulated"] is False and live["price_source"] == "Alpaca market data (IEX)"
    assert "simulated prices" not in live["price_note"] and "Alpaca" in live["price_note"]


def test_default_benchmark_per_feed():
    syms = {"MKTX", "SPY"}
    assert default_benchmark(type("S", (), {"simulated": True})(), syms.__contains__) == "MKTX"
    assert default_benchmark(LiveLike(), syms.__contains__) == "SPY"  # MKTX is a real company there
    assert default_benchmark(LiveLike(), {"AAPL"}.__contains__) == ""


def _client(simulated):
    ex, feed, clock = make_exchange(None, ABC="100.00", MKTX="400.00", SPY="500.00")
    feed.simulated = simulated
    if not simulated:
        feed.status_info = lambda: {"data_source": "Alpaca", "data_exchange": "IEX", "fallback_reason": None}
    a = ex.create_account("Margin", "margin").id
    ex.submit(a, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(ex, feed, clock, "SPY", "510.00", seconds=61)
    return TestClient(create_app(ex, run_loop=False)), ex, a


def test_live_feed_reports_are_not_called_simulated():
    c, ex, a = _client(False)
    an = c.get(f"/api/accounts/{a}/analytics").json()
    assert an["simulated"] is False and an["benchmark_default"] == "SPY" and an["benchmark"]["symbol"] == "SPY"
    assert "simulated prices" not in an["price_note"]
    month = c.get(f"/api/accounts/{a}/statements").json()["months"][-1]
    st = c.get(f"/api/accounts/{a}/statements/{month}").json()
    assert st["simulated"] is False and "Alpaca market data (IEX)" in st["notes"][0]
    assert not any("All prices are simulated" in n for n in st["notes"])
    html = c.get(f"/api/accounts/{a}/statements/{month}/html").text
    assert "real market prices, simulated trades" in html and "All prices are simulated" not in html
    status = c.get("/api/status").json()
    assert status["price_note"] == an["price_note"]
    # the user's choice still wins
    assert c.get(f"/api/accounts/{a}/analytics", params={"benchmark": "MKTX"}).json()["benchmark"]["symbol"] == "MKTX"


def test_simulated_feed_reports_say_simulated():
    c, ex, a = _client(True)
    an = c.get(f"/api/accounts/{a}/analytics").json()
    assert an["simulated"] is True and an["benchmark_default"] == "MKTX"
    assert c.get("/api/status").json()["price_note"] == "All figures use simulated prices."
    month = c.get(f"/api/accounts/{a}/statements").json()["months"][-1]
    st = c.get(f"/api/accounts/{a}/statements/{month}").json()
    assert st["simulated"] and st["notes"][0].startswith("All prices are simulated.")
    assert "simulated prices, no real money" in c.get(f"/api/accounts/{a}/statements/{month}/html").text
