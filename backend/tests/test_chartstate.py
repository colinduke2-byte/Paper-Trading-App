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
    assert c.get("/api/chartstate").json() == {"scope": "live", "settings": None, "drawings": {}}
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
        assert json.load(f)["scopes"]["live"]["TECX"]["data"] == HLINE
    # A new server on the same directory (a restart) reads it back.
    c2 = client(str(tmp_path))
    assert c2.get("/api/chartstate/drawings/TECX").json()["data"] == HLINE


def test_reset(tmp_path):
    c = client(str(tmp_path))
    c.put("/api/chartstate/settings", json={"updated": 1, "data": {"type": "bars"}})
    c.put("/api/chartstate/drawings/TECX", json={"updated": 1, "data": HLINE})
    assert c.delete("/api/chartstate").json() == {"settings": None, "scopes": {}}
    assert client(str(tmp_path)).get("/api/chartstate").json() == {"scope": "live", "settings": None, "drawings": {}}


def test_drawings_are_kept_per_scope_and_settings_are_shared():
    c = client()
    c.put("/api/chartstate/settings", json={"updated": 1, "data": {"type": "line"}})
    c.put("/api/chartstate/drawings/MKTX", json={"updated": 1, "data": HLINE})
    other = [{"id": "r", "type": "vline", "points": [{"t": 5, "p": 1}]}]
    assert c.put("/api/chartstate/drawings/MKTX?scope=replay:sample-1", json={"updated": 2, "data": other}).status_code == 200
    assert c.get("/api/chartstate/drawings/MKTX").json()["data"] == HLINE
    assert c.get("/api/chartstate/drawings/MKTX?scope=live").json()["data"] == HLINE
    assert c.get("/api/chartstate/drawings/MKTX?scope=replay:sample-1").json()["data"] == other
    assert c.get("/api/chartstate/drawings/MKTX?scope=replay:other").json() is None
    st = c.get("/api/chartstate?scope=replay:sample-1").json()
    assert st["scope"] == "replay:sample-1" and st["drawings"]["MKTX"]["data"] == other
    assert st["settings"]["data"] == {"type": "line"}
    assert c.get("/api/chartstate/drawings/MKTX?scope=bogus").status_code == 400
    assert c.get("/api/chartstate/drawings/MKTX?scope=replay:../x").status_code == 400


def test_version_1_file_is_read_as_live_drawings(tmp_path):
    (tmp_path / "chartstate.json").write_text(json.dumps(
        {"settings": {"updated": 1, "data": {"type": "bars"}}, "drawings": {"TECX": {"updated": 3, "data": HLINE}}}))
    c = client(str(tmp_path))
    assert c.get("/api/chartstate/drawings/TECX").json()["data"] == HLINE
    assert c.get("/api/chartstate").json()["settings"]["data"] == {"type": "bars"}


def test_replay_saves_go_to_the_live_data_directory(tmp_path):
    from app.marketdata.replay.samples import SAMPLES
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path / "live"))
    ex.create_account("Margin", "margin")
    c = TestClient(create_app(ex, run_loop=False))
    c.put("/api/chartstate/drawings/MKTX", json={"updated": 1, "data": HLINE})
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 200
    scope = f"replay:{c.get('/api/replay/status').json()['session_id']}"
    sym = c.get("/api/symbols").json()[0]["symbol"]
    assert c.put(f"/api/chartstate/drawings/{sym}?scope={scope}", json={"updated": 2, "data": HLINE}).status_code == 200
    # Live drawings are still there while the replay runs, and nothing is written into the sandbox.
    assert c.get("/api/chartstate/drawings/MKTX").json()["data"] == HLINE
    assert not (tmp_path / "live" / "replay" / "chartstate.json").exists()
    assert c.post("/api/replay/exit").status_code == 200
    saved = json.loads((tmp_path / "live" / "chartstate.json").read_text())
    assert saved["scopes"]["live"]["MKTX"]["data"] == HLINE
    assert saved["scopes"][scope][sym]["data"] == HLINE
    # A second run of the same session (the sandbox is wiped on start) still finds its drawings.
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 200
    assert c.get(f"/api/chartstate/drawings/{sym}?scope={scope}").json()["data"] == HLINE


def test_chartstate_needs_a_session_when_a_passcode_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERDESK_PASSCODE", "correct horse battery")
    monkeypatch.setenv("PAPERDESK_LOOPBACK_BYPASS", "0")
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=str(tmp_path))
    ex.create_account("Margin", "margin")
    c = TestClient(create_app(ex, run_loop=False), client=("127.0.0.1", 50000), base_url="http://127.0.0.1:8000")
    origin = {"origin": "http://127.0.0.1:8000"}
    body = {"updated": 1, "data": HLINE}
    assert c.get("/api/chartstate").status_code == 401
    assert c.put("/api/chartstate/drawings/MKTX", json=body, headers=origin).status_code == 401
    assert c.delete("/api/chartstate", headers=origin).status_code == 401
    assert c.post("/api/auth/login", json={"passcode": "correct horse battery"}, headers=origin).json() == {"ok": True}
    assert c.put("/api/chartstate/drawings/MKTX", json=body, headers=origin).status_code == 200
    # A write from another site is refused even with the session cookie.
    assert c.put("/api/chartstate/drawings/MKTX", json={"updated": 2, "data": []}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/chartstate/drawings/MKTX").json()["data"] == HLINE
