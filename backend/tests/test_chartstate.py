import json
import time

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed


def client(data_dir=None):
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=data_dir)
    ex.create_account("Margin", "margin")
    return TestClient(create_app(ex, run_loop=False))


HLINE = [{"id": "a", "type": "hline", "points": [{"t": 1700000000, "p": 101.5}]}]


def test_empty_state():
    c = client()
    assert c.get("/api/chartstate").json() == {"settings": None, "drawings": {}}
    assert c.get("/api/chartstate/drawings/MKTX").json() is None


def test_settings_and_drawings_round_trip():
    c = client()
    s = {"updated": 10, "data": {"type": "line", "indicators": [{"type": "sma", "params": {"period": 30}}]}}
    assert c.put("/api/chartstate/settings", json=s).json() == s
    r = c.put("/api/chartstate/drawings/mktx", json={"updated": 5, "data": HLINE})
    assert r.status_code == 200
    state = c.get("/api/chartstate").json()
    assert state["settings"] == s
    assert state["drawings"]["MKTX"] == {"updated": 5, "data": HLINE}
    assert c.get("/api/chartstate/drawings/MKTX").json()["data"] == HLINE


def test_older_record_does_not_overwrite_newer():
    c = client()
    c.put("/api/chartstate/drawings/MKTX", json={"updated": 20, "data": HLINE})
    r = c.put("/api/chartstate/drawings/MKTX", json={"updated": 10, "data": []})
    assert r.json() == {"updated": 20, "data": HLINE}
    assert c.put("/api/chartstate/drawings/MKTX", json={"updated": 30, "data": []}).json()["data"] == []


def test_validation():
    c = client()
    assert c.put("/api/chartstate/settings", json={"data": {}}).status_code == 400
    assert c.put("/api/chartstate/settings", json={"updated": "x", "data": {}}).status_code == 400
    assert c.put("/api/chartstate/settings", json={"updated": True, "data": {}}).status_code == 400
    assert c.put("/api/chartstate/settings", json={"updated": 1}).status_code == 400
    assert c.put("/api/chartstate/drawings/MKTX", json={"updated": 1, "data": {}}).status_code == 400
    assert c.put("/api/chartstate/drawings/BAD$SYM", json={"updated": 1, "data": []}).status_code == 400
    big = [{"id": str(i), "type": "text", "points": [{"t": 0, "p": 1}], "text": "x" * 200} for i in range(2000)]
    assert c.put("/api/chartstate/drawings/MKTX", json={"updated": 1, "data": big}).status_code == 413


def test_persisted_to_the_data_directory(tmp_path):
    c = client(str(tmp_path))
    c.put("/api/chartstate/drawings/TECX", json={"updated": 1, "data": HLINE})
    with open(tmp_path / "chartstate.json", encoding="utf-8") as f:
        assert json.load(f)["drawings"]["TECX"]["data"] == HLINE
    # A new server on the same directory (a restart) reads it back.
    c2 = client(str(tmp_path))
    assert c2.get("/api/chartstate/drawings/TECX").json()["data"] == HLINE


def test_reset(tmp_path):
    c = client(str(tmp_path))
    c.put("/api/chartstate/settings", json={"updated": 1, "data": {"type": "bars"}})
    c.put("/api/chartstate/drawings/TECX", json={"updated": 1, "data": HLINE})
    assert c.delete("/api/chartstate").json() == {"settings": None, "drawings": {}}
    assert client(str(tmp_path)).get("/api/chartstate").json() == {"settings": None, "drawings": {}}
