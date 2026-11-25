"""Alpaca configured but unusable at start-up: the app says so in /api/status, which the UI shows."""
import time

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.exchange import Exchange
from app.marketdata import config
from app.marketdata.synthetic import SyntheticFeed
from test_alpaca import FakeTransport

NOW = 1795500000.0


def test_rejected_key_sets_live_unavailable_with_a_short_cause(tmp_path):
    env = {"APCA_API_KEY_ID": "PKBAD", "APCA_API_SECRET_KEY": "bad"}
    tr = FakeTransport({"/v2/assets": (401, {"message": "unauthorized"})})
    sel = config.select_feed(NOW, str(tmp_path), environ=env, dotenv=tmp_path / "none", start=False,
                             transport=tr, sleep=lambda s: None)
    assert isinstance(sel.feed, SyntheticFeed) and sel.data_dir == str(tmp_path)
    assert sel.feed.live_unavailable == "the API key was rejected, HTTP 401"
    assert "PKBAD" not in sel.feed.live_unavailable


def test_unreachable_sets_live_unavailable(tmp_path):
    tr = FakeTransport({})
    tr.get = lambda *a: (_ for _ in ()).throw(OSError("timed out"))
    sel = config.select_feed(NOW, str(tmp_path), environ={"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s"},
                             dotenv=tmp_path / "none", start=False, transport=tr, sleep=lambda s: None)
    assert sel.feed.live_unavailable.startswith("it could not be reached: ")


def test_no_keys_is_not_a_failure(tmp_path):
    sel = config.select_feed(NOW, str(tmp_path), environ={}, dotenv=tmp_path / "none")
    assert sel.feed.live_unavailable is None


def test_status_reports_live_unavailable():
    feed = SyntheticFeed(seed=3, now=time.time())
    ex = Exchange(feed)
    ex.create_account("M", "margin")
    c = TestClient(create_app(ex, run_loop=False))
    assert c.get("/api/status").json()["live_unavailable"] is None
    feed.live_unavailable = "the API key was rejected, HTTP 401"
    assert c.get("/api/status").json()["live_unavailable"] == "the API key was rejected, HTTP 401"
