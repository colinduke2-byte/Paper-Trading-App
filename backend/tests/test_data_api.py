"""Data and backups API (routes_data.py) with access control on: sign-in, Origin checks, uploads, recovery."""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.exchange import Exchange
from app.marketdata.replay.samples import SAMPLES
from app.marketdata.synthetic import SyntheticFeed
from app.storage import archive, schema

PASS = "correct horse 42"
SAME = {"origin": "http://testserver", "sec-fetch-site": "same-origin"}
ZIP = {**SAME, "content-type": "application/zip"}


def build(tmp_path, monkeypatch, passcode=True, prepare=None):
    if passcode:
        monkeypatch.setenv("PAPERDESK_PASSCODE", PASS)
    d = tmp_path / "data"
    if prepare:
        prepare(d)
    ex = Exchange(SyntheticFeed(seed=3, now=1_795_500_000.0), data_dir=str(d))
    if not ex.load() and not ex.store.blocked:
        ex.create_account("Margin", "margin")
        ex.create_account("Cash", "cash")
    ex.tick()
    app = create_app(ex, run_loop=False)
    c = TestClient(app, client=("192.168.1.20", 50000))
    if passcode:
        assert c.post("/api/auth/login", json={"passcode": PASS}, headers=SAME).json() == {"ok": True}
    return c, ex, app


def trade(c, aid, n=3):
    for i in range(n):
        r = c.post(f"/api/accounts/{aid}/orders", json={"symbol": "MKTX", "side": "buy", "qty": 1 + i}, headers=SAME)
        assert r.status_code == 200, r.text


def test_every_data_route_needs_a_session_and_writes_need_same_origin(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    anon = TestClient(app, client=("192.168.1.30", 50000))
    for method, path in [("get", "/api/data/status"), ("get", "/api/data/backups"), ("post", "/api/data/backups"),
                         ("get", "/api/data/export"), ("post", "/api/data/restore/preview"),
                         ("post", "/api/data/restore/apply"), ("get", "/api/data/accounts/A0001/export"),
                         ("post", "/api/data/accounts/import/preview"), ("post", "/api/data/recovery/fresh"),
                         ("post", "/api/data/recovery/quit"), ("delete", "/api/data/backups/x")]:
        r = getattr(anon, method)(path, headers=SAME)
        assert r.status_code == 401, (method, path, r.status_code)
    # signed in, but from another site: refused before the route runs
    for path in ("/api/data/backups", "/api/data/restore/preview", "/api/data/recovery/fresh"):
        assert c.post(path, headers={"origin": "http://evil.example"}).status_code == 403
        assert c.post(path, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.get("/api/data/status").status_code == 200


def test_status_reports_saves_and_backups(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    ex.save()
    s = c.get("/api/data/status").json()
    assert s["enabled"] and s["save"]["last_save"] and not s["save"]["saving_failed"] and s["recovery"] is None
    assert s["schema_version"] == schema.CURRENT_VERSION and s["backups"]["count"] == 0
    brief = c.get("/api/status").json()["storage"]
    assert brief["saving_failed"] is False and brief["recovery"] is None


def test_manual_backup_download_and_restore_with_dry_run(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    r = c.post("/api/data/backups", headers=SAME)
    assert r.status_code == 200 and r.json()["kind"] == "manual"
    name = r.json()["name"]
    # download it
    r = c.get(f"/api/data/backups/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f'filename="{name}"' in r.headers["content-disposition"]
    saved = r.content
    assert c.get("/api/data/backups/..%2Fstate.json").status_code == 404
    # change things after the backup
    cash_before = c.get("/api/accounts/A0001").json()["cash"]
    trade(c, "A0001", 2)
    c.post("/api/accounts", json={"name": "Later", "type": "cash", "starting_cash": 50}, headers=SAME)
    # dry run: nothing changes yet
    r = c.post("/api/data/restore/preview", content=saved, headers=ZIP)
    assert r.status_code == 200, r.text
    p = r.json()
    changes = {a["id"]: a["change"] for a in p["accounts"]}
    assert changes == {"A0001": "replaced", "A0002": "unchanged", "A0003": "removed"}
    assert p["source"] == "uploaded file" and p["token"] and p["missing_symbols"] == []
    assert len(c.get("/api/accounts").json()) == 3
    # apply: a snapshot of the current state is taken first
    r = c.post("/api/data/restore/apply", json={"token": p["token"]}, headers=SAME)
    assert r.status_code == 200 and r.json()["saved"] is True
    assert [a["id"] for a in c.get("/api/accounts").json()] == ["A0001", "A0002"]
    assert c.get("/api/accounts/A0001").json()["cash"] == cash_before
    kinds = [b["kind"] for b in c.get("/api/data/backups").json()]
    assert "pre-restore" in kinds
    # the token was used up
    assert c.post("/api/data/restore/apply", json={"token": p["token"]}, headers=SAME).status_code == 409
    # and the restored state is what is on disk
    ex2 = Exchange(SyntheticFeed(seed=3, now=1_795_500_000.0), data_dir=str(tmp_path / "data"))
    assert ex2.load() and sorted(ex2.accounts) == ["A0001", "A0002"]


def test_restore_preview_from_a_listed_backup(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    name = c.post("/api/data/backups", headers=SAME).json()["name"]
    r = c.post("/api/data/restore/preview", json={"backup": name}, headers=SAME)
    assert r.status_code == 200 and r.json()["source"] == f"backup {name}"
    assert c.post("/api/data/restore/preview", json={"backup": "../../x.zip"}, headers=SAME).status_code == 404


def test_full_export_holds_everything_but_sign_ins(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    sessions = tmp_path / "data" / "sessions"
    sessions.mkdir()
    (sessions / "2026-11-20-mine.pdsession.gz").write_bytes(b"recorded")
    r = c.get("/api/data/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
    assert names == {"manifest.json", "state.json", "market.json", "sessions/2026-11-20-mine.pdsession.gz"}
    assert manifest["schema_version"] == schema.CURRENT_VERSION and manifest["app_version"]
    assert (tmp_path / "data" / "sessions.json").exists()  # sign-ins exist, but are not exported
    arc = archive.read(r.content)
    assert len(arc.accounts) == 2


def test_uploads_are_size_limited_and_typed(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    monkeypatch.setattr(archive, "MAX_UPLOAD", 1000)
    r = c.post("/api/data/restore/preview", content=b"PK" + b"0" * 5000, headers=ZIP)
    assert r.status_code == 413 and "too large" in r.json()["detail"]
    r = c.post("/api/data/restore/preview", content=b"hello", headers={**SAME, "content-type": "text/plain"})
    assert r.status_code == 415
    r = c.post("/api/data/restore/preview", content=b"PK\x03\x04junk", headers=ZIP)
    assert r.status_code == 422 and "damaged" in r.json()["detail"]
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("state.json", "{}")
        z.writestr("../../escape.txt", "x")
    r = c.post("/api/data/restore/preview", content=bad.getvalue(), headers=ZIP)
    assert r.status_code == 422 and "cannot place safely" in r.json()["detail"]
    assert not (tmp_path / "escape.txt").exists()


def test_reset_and_delete_take_a_snapshot_first(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    assert c.post("/api/accounts/A0001/reset", json={}, headers=SAME).status_code == 200
    assert c.delete("/api/accounts/A0002", headers=SAME).status_code == 200
    kinds = sorted(b["kind"] for b in c.get("/api/data/backups").json())
    assert kinds == ["pre-delete", "pre-reset"]
    pre = [b for b in c.get("/api/data/backups").json() if b["kind"] == "pre-reset"][0]
    arc = app.state.data.backups.read(pre["name"])
    assert len(arc.doc["accounts"][0]["fills"]) == 3  # the trades from before the reset are in it


def test_account_export_and_import_over_the_api(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    r = c.get("/api/data/accounts/A0001/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    exported = r.content
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    c2, ex2, app2 = build(other_dir, monkeypatch)
    r = c2.post("/api/data/accounts/import/preview", content=exported, headers={**SAME, "content-type": "application/json"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["old_id"] == "A0001" and p["new_id"] == "A0003" and p["fills"] == 3 and p["missing_symbols"] == []
    r = c2.post("/api/data/accounts/import/apply", json={"token": p["token"], "name": "From laptop"}, headers=SAME)
    assert r.status_code == 200 and r.json()["account_id"] == "A0003"
    accts = {a["id"]: a for a in c2.get("/api/accounts").json()}
    assert accts["A0003"]["name"] == "From laptop"
    assert accts["A0003"]["cash"] == c.get("/api/accounts/A0001").json()["cash"]
    assert "pre-import" in [b["kind"] for b in c2.get("/api/data/backups").json()]
    r = c2.post("/api/data/accounts/import/preview", content=exported.replace(b"MKTX", b"MKTY"),
                headers={**SAME, "content-type": "application/json"})
    assert r.status_code == 422 and "checksum" in r.json()["detail"]


def test_backups_and_restores_wait_while_a_replay_runs(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch)
    name = c.post("/api/data/backups", headers=SAME).json()["name"]
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}, headers=SAME).status_code == 200
    assert c.post("/api/data/backups", headers=SAME).status_code == 409
    r = c.post("/api/data/restore/preview", json={"backup": name}, headers=SAME)
    assert r.status_code == 409 and "replay" in r.json()["detail"]
    svc = app.state.data
    svc.backups.interval = 1
    assert svc.tick() is None  # no automatic backup during a replay
    assert c.post("/api/replay/exit", headers=SAME).status_code == 200
    assert svc.tick()["kind"] == "auto"


# ---- recovery ----------------------------------------------------------------------------------------

def damage(d):
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_bytes(b'{"version": 2, "accounts": [{"id": "A0001", "na')


def test_recovery_screen_choices_restore_latest(tmp_path, monkeypatch):
    # first run: save some state and a backup
    c, ex, app = build(tmp_path, monkeypatch)
    trade(c, "A0001")
    c.post("/api/data/backups", headers=SAME)
    ex.save(force=True)
    good_cash = c.get("/api/accounts/A0001").json()["cash"]
    damage(tmp_path / "data")
    # second run: the damaged file is detected
    c, ex, app = build(tmp_path, monkeypatch)
    st = c.get("/api/status").json()["storage"]
    assert st["recovery"]["kind"] == "damaged" and "not valid JSON" in st["recovery"]["reason"]
    rec = c.get("/api/data/recovery").json()
    assert rec["recovery"]["copy"] and len(rec["backups"]) >= 1
    assert c.get("/api/accounts").json() == []
    assert c.post("/api/data/backups", headers=SAME).status_code == 409  # nothing in memory worth backing up
    r = c.post("/api/data/recovery/restore", json={}, headers=SAME)
    assert r.status_code == 200, r.text
    assert c.get("/api/status").json()["storage"]["recovery"] is None
    assert c.get("/api/accounts/A0001").json()["cash"] == good_cash
    copy = rec["recovery"]["copy"]
    assert open(os.path.join(copy, "state.json"), "rb").read().startswith(b'{"version": 2, "accounts": [{"id"')


def test_recovery_start_fresh_and_quit(tmp_path, monkeypatch):
    c, ex, app = build(tmp_path, monkeypatch, prepare=damage)
    assert c.post("/api/data/recovery/restore", json={}, headers=SAME).status_code == 409  # there are no backups
    calls = []
    monkeypatch.setattr("app.api.routes_data._shutdown", lambda: calls.append(1))
    r = c.post("/api/data/recovery/quit", headers=SAME)
    assert r.status_code == 200 and "Nothing was changed" in r.json()["detail"]
    import time
    time.sleep(0.5)
    assert calls == [1]
    assert (tmp_path / "data" / "state.json").read_bytes().startswith(b'{"version": 2, "accounts": [{"id": "A0001", "na')
    r = c.post("/api/data/recovery/fresh", headers=SAME)
    assert r.status_code == 200 and r.json()["accounts"] == 2
    assert json.loads((tmp_path / "data" / "state.json").read_text())["version"] == schema.CURRENT_VERSION
    assert c.post("/api/data/recovery/fresh", headers=SAME).status_code == 409  # not in recovery any more
    assert c.post("/api/data/recovery/quit", headers=SAME).status_code == 409


def test_newer_state_is_refused_without_a_passcode_too(tmp_path, monkeypatch):
    def newer(d):
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"version": 99, "accounts": []}))
    c, ex, app = build(tmp_path, monkeypatch, passcode=False, prepare=newer)
    st = c.get("/api/status").json()["storage"]
    assert st["recovery"]["kind"] == "newer" and "newer PaperDesk" in st["recovery"]["reason"]
    with c:
        pass  # start and stop the app: the shutdown save must not overwrite the file
    assert json.loads((tmp_path / "data" / "state.json").read_text())["version"] == 99
