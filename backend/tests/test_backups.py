"""Rotating backups, backup archives (including hostile uploads) and moving one account between installations."""
import io
import json
import os
import zipfile
from datetime import datetime, timezone

import pytest

from app.core.clock import SimClock
from app.exchange import Exchange
from app.marketdata.feed import SimFeed
from app.storage import archive, schema
from app.storage.archive import ArchiveError
from app.storage.backups import BackupError, BackupManager
from helpers import D, T0, make_exchange, move


def busy(tmp_path=None, symbol="ABC"):
    ex, feed, clock = make_exchange(str(tmp_path) if tmp_path else None, **{symbol: "100.00"})
    a = ex.create_account("Main", "margin", D("100000")).id
    for i in range(4):
        ex.submit(a, {"symbol": symbol, "side": "buy" if i % 2 == 0 else "sell", "qty": 3 if i % 2 == 0 else 1})
        move(ex, feed, clock, symbol, "100.00")
    ex.submit(a, {"symbol": symbol, "side": "buy", "qty": 1, "type": "limit", "limit_price": "80", "tif": "gtc"})
    ex.watchlists["Main"] = [symbol]
    return ex, feed, clock, a


INFO = {"app_version": "0.1.0", "schema_version": schema.CURRENT_VERSION, "feed": "scripted", "simulated": True}


def make_archive(ex, **kw) -> bytes:
    return archive.build(ex.to_document(), {"quotes": {}}, kw.pop("sessions", None),
                         {"kind": "manual", "created_ts": 1_800_000_000.0, **INFO, **kw})


def zip_of(files: dict, manifest=None, fix_manifest=True) -> bytes:
    import hashlib
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if manifest is None and fix_manifest:
            manifest = {"format": "paperdesk-backup", "format_version": 1, "schema_version": 2,
                        "files": {n: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for n, b in files.items()}}
        if manifest is not None:
            z.writestr("manifest.json", json.dumps(manifest))
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


# ---- archives -------------------------------------------------------------------------------------

def test_archive_round_trip():
    ex, feed, clock, a = busy()
    data = make_archive(ex, sessions={"2026-11-20-mine.pdreplay": b"x" * 100})
    arc = archive.read(data)
    assert arc.manifest["format"] == "paperdesk-backup" and arc.manifest["schema_version"] == schema.CURRENT_VERSION
    assert set(arc.manifest["files"]) == {"state.json", "market.json", "sessions/2026-11-20-mine.pdreplay"}
    assert json.dumps(arc.doc, sort_keys=True) == json.dumps(ex.to_document(), sort_keys=True)
    assert arc.sessions == {"2026-11-20-mine.pdreplay": b"x" * 100} and arc.market == {"quotes": {}}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "sessions.json" not in z.namelist() and not any(".env" in n for n in z.namelist())


def good_state(ex) -> bytes:
    return json.dumps(ex.to_document()).encode()


@pytest.mark.parametrize("name", ["../evil.json", "/etc/passwd", "sessions/../../x", "sessions\\..\\x", "C:/x",
                                  "sessions/", "state.json/x", "other.txt", ".env", "sessions.json",
                                  "sessions/.hidden"])
def test_hostile_member_names_are_refused(name):
    ex, *_ = busy()
    data = zip_of({"state.json": good_state(ex), name: b"x"})
    with pytest.raises(ArchiveError, match="cannot place safely|does not use|folder or link"):
        archive.read(data)


def test_symlinks_encryption_and_duplicates_are_refused():
    ex, *_ = busy()
    st = good_state(ex)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("state.json", st)
        info = zipfile.ZipInfo("sessions/link")
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveError, match="folder or link"):
        archive.read(buf.getvalue())
    data = bytearray(zip_of({"state.json": st}))
    pos = data.find(b"PK\x01\x02")
    while pos >= 0:  # set the "encrypted" bit in every central directory entry
        data[pos + 8] |= 0x1
        pos = data.find(b"PK\x01\x02", pos + 4)
    with pytest.raises(ArchiveError, match="encrypted"):
        archive.read(bytes(data))
    buf = io.BytesIO()
    import warnings
    with zipfile.ZipFile(buf, "w") as z, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        z.writestr("manifest.json", "{}")
        z.writestr("state.json", st)
        z.writestr("state.json", st)
    with pytest.raises(ArchiveError, match="twice"):
        archive.read(buf.getvalue())


def test_decompression_bombs_are_refused_before_unpacking():
    bomb = zip_of({"state.json": b"{" + b" " * (40 * 1024 * 1024) + b"}"})
    assert len(bomb) < 200_000
    with pytest.raises(ArchiveError, match="zip bomb"):
        archive.read(bomb)


def test_declared_sizes_that_lie_are_caught():
    """A header that claims a small size: reading stops at the claimed size instead of trusting it."""
    ex, *_ = busy()
    st = good_state(ex)
    data = bytearray(zip_of({"state.json": st}))
    # patch the central directory's uncompressed size of state.json to 10 bytes
    pos = 0
    while True:
        pos = data.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        name_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        if bytes(data[pos + 46:pos + 46 + name_len]) == b"state.json":
            data[pos + 24:pos + 28] = (10).to_bytes(4, "little")
        pos += 4
    with pytest.raises(ArchiveError):
        archive.read(bytes(data))


def test_uploads_over_the_size_limit_and_non_zips_are_refused():
    with pytest.raises(ArchiveError, match="up to 64 MB"):
        archive.read(b"PK" + b"\0" * (archive.MAX_UPLOAD + 1))
    with pytest.raises(ArchiveError, match="not a zip"):
        archive.read(b"{\"version\": 2}")
    with pytest.raises(ArchiveError, match="damaged"):
        archive.read(b"PK\x03\x04garbage")
    many = {f"sessions/s{i}": b"" for i in range(archive.MAX_MEMBERS + 1)}
    with pytest.raises(ArchiveError, match="far fewer"):
        archive.read(zip_of(many))


def test_checksums_manifest_and_versions_are_checked():
    ex, *_ = busy()
    good = make_archive(ex)
    with zipfile.ZipFile(io.BytesIO(good)) as z:
        files = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(files.pop("manifest.json"))

    def rebuild(fs, man):
        return zip_of(fs, manifest=man)
    # a changed byte
    bad = dict(files, **{"state.json": files["state.json"].replace(b'"Main"', b'"Mainx"', 1)})
    with pytest.raises(ArchiveError, match="checksum"):
        archive.read(rebuild(bad, manifest))
    # an extra file the manifest does not list
    with pytest.raises(ArchiveError, match="do not match its manifest"):
        archive.read(rebuild(dict(files, **{"sessions/x": b"1"}), manifest))
    # wrong format, newer format, no manifest
    with pytest.raises(ArchiveError, match="wrong format"):
        archive.read(rebuild(files, dict(manifest, format="zip")))
    with pytest.raises(ArchiveError, match="newer PaperDesk"):
        archive.read(rebuild(files, dict(manifest, format_version=99)))
    with pytest.raises(ArchiveError, match="manifest.json or state.json is missing"):
        archive.read(zip_of(files, fix_manifest=False))
    # a state from a newer PaperDesk
    d = json.loads(files["state.json"])
    d["version"] = schema.CURRENT_VERSION + 1
    with pytest.raises(ArchiveError, match="newer PaperDesk"):
        archive.read(zip_of({"state.json": json.dumps(d).encode()}))
    # segment references (files outside the archive)
    d = json.loads(files["state.json"])
    d["accounts"][0]["ledger"] = {"$segment": "segments/x.ledger.jsonl", "count": 1}
    with pytest.raises(ArchiveError, match="outside it"):
        archive.read(zip_of({"state.json": json.dumps(d).encode()}))
    # inconsistent books
    d = json.loads(files["state.json"])
    d["accounts"][0]["lots"]["ABC"][0][3] = "1.00"
    with pytest.raises(ArchiveError, match="inconsistent"):
        archive.read(zip_of({"state.json": json.dumps(d).encode()}))


def test_old_version_archives_are_migrated():
    raw = open(os.path.join(os.path.dirname(__file__), "fixtures", "states", "v1-f194465-first-build", "state.json"), "rb").read()
    arc = archive.read(zip_of({"state.json": raw}))
    assert arc.from_version == 1 and arc.doc["version"] == schema.CURRENT_VERSION


# ---- automatic backups -----------------------------------------------------------------------------

def test_backup_is_written_verified_and_listed(tmp_path):
    ex, feed, clock, a = busy(tmp_path)
    bm = BackupManager(str(tmp_path), wall=lambda: 1_800_000_000.0, interval=3600)
    row = bm.create("auto", ex.to_document(), None, INFO)
    assert row["name"] == "paperdesk-auto-20270115T080000Z.zip" and row["kind"] == "auto"
    assert [r["name"] for r in bm.list()] == [row["name"]]
    arc = bm.read(row["name"])
    assert json.dumps(arc.doc, sort_keys=True) == json.dumps(ex.to_document(), sort_keys=True)
    assert not bm.due()
    row2 = bm.create("auto", ex.to_document(), None, INFO)
    assert row2["name"] == "paperdesk-auto-20270115T080000Z-2.zip"
    with pytest.raises(KeyError):
        bm.path_of("../state.json")
    with pytest.raises(KeyError):
        bm.path_of("paperdesk-auto-20270115T080000Z-9.zip")


def test_a_backup_that_fails_its_check_is_deleted(tmp_path, monkeypatch):
    ex, *_ = busy(tmp_path)
    bm = BackupManager(str(tmp_path), interval=3600)

    def corrupt(path, data):
        with open(path, "wb") as f:
            f.write(data[:-30])
    monkeypatch.setattr("app.storage.fsutil.atomic_write_bytes", corrupt)
    with pytest.raises(BackupError, match="failed its check"):
        bm.create("auto", ex.to_document(), None, INFO)
    assert bm.list() == [] and "failed its check" in bm.last_error


def test_pruning_keeps_24_hourly_14_daily_events_and_manual(tmp_path):
    ex, *_ = busy(tmp_path)
    doc = ex.to_document()
    now = [datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()]
    bm = BackupManager(str(tmp_path), wall=lambda: now[0], interval=1800)
    bm.create("manual", doc, None, INFO)
    for i in range(10):
        bm.create("pre-reset", doc, None, INFO)
        now[0] += 1
    for i in range(30 * 48):  # every 30 minutes for 30 days
        now[0] += 1800
        bm.create("auto", doc, None, INFO)
    rows = bm.list()
    autos = [r for r in rows if r["kind"] == "auto"]
    hours = {int(r["created_ts"] // 3600) for r in autos}
    days = {datetime.fromtimestamp(r["created_ts"], timezone.utc).date() for r in autos}
    newest = max(r["created_ts"] for r in autos)
    assert len(autos) <= 24 + 14 and len(hours) >= 24 and len(days) == 14
    assert all(newest - r["created_ts"] < 14 * 86400 for r in autos)
    assert sum(1 for r in rows if r["kind"] == "pre-reset") == 10 and sum(1 for r in rows if r["kind"] == "manual") == 1
    # files that are not ours are never touched
    (tmp_path / "backups" / "my-notes.zip").write_bytes(b"x")
    bm.prune()
    assert (tmp_path / "backups" / "my-notes.zip").exists()


def test_event_snapshots_are_capped(tmp_path):
    ex, *_ = busy(tmp_path)
    now = [1_800_000_000.0]
    bm = BackupManager(str(tmp_path), wall=lambda: now[0], keep_events=5)
    for i in range(9):
        now[0] += 1
        bm.create("pre-delete", ex.to_document(), None, INFO)
    assert len(bm.list()) == 5


# ---- one account between installations -------------------------------------------------------------

def test_account_export_and_import_remaps_every_id(tmp_path):
    src, feed, clock, a = busy()
    data = archive.export_account(src, a, {"app_version": "0.1.0", "created": "x", "feed": "scripted", "simulated": True})
    d = archive.read_account(data)
    dst, feed2, clock2 = make_exchange(None)
    b = dst.create_account("Main", "margin", D("5000")).id
    dst.submit(b, {"symbol": "ABC", "side": "buy", "qty": 1})
    move(dst, feed2, clock2, "ABC", "100.00")
    imp = archive.prepare_import(dst, d)
    assert imp.old_id == a and imp.new_id != b and imp.missing_symbols == []
    new = archive.apply_import(dst, imp)
    acc = dst.account(new)
    assert acc.name == "Main (imported)" and schema.account_problems(acc) == []
    assert acc.cash == src.account(a).cash and acc.positions() == src.account(a).positions()
    fill_ids = [f.id for x in dst.accounts.values() for f in x.fills]
    assert len(fill_ids) == len(set(fill_ids))
    assert all(f.account_id == new for f in acc.fills) and all(o.account_id in dst.accounts for o in dst.engine.orders.values())
    assert {t.ref for t in acc.ledger.transactions if t.ref} <= {f.id for f in acc.fills}
    for o in dst.engine.orders.values():
        if o.account_id == new:
            assert o.id not in {x.id for x in src.engine.orders.values()} or o.id.startswith("O")
    # the imported working order keeps working, and new orders do not collide
    assert any(o.active for o in dst.engine.orders.values() if o.account_id == new)
    [o] = dst.submit(new, {"symbol": "ABC", "side": "buy", "qty": 1})
    assert list(dst.engine.orders).count(o.id) == 1
    move(dst, feed2, clock2, "ABC", "100.00")
    assert schema.account_problems(dst.account(new)) == []


def test_import_into_a_feed_without_the_symbols_says_so_and_cancels_their_orders():
    src, feed, clock, a = busy(symbol="ZZZQ")
    d = archive.read_account(archive.export_account(src, a, {}))
    dst, *_ = make_exchange(None)  # only ABC exists here
    imp = archive.prepare_import(dst, d)
    assert imp.missing_symbols == ["ZZZQ"] and len(imp.canceled) == 1
    new = archive.apply_import(dst, imp)
    assert not any(o.active for o in dst.engine.orders.values() if o.account_id == new)
    assert dst.account(new).position("ZZZQ") == 4


def test_damaged_or_foreign_account_files_are_refused():
    src, feed, clock, a = busy()
    data = archive.export_account(src, a, {})
    with pytest.raises(ArchiveError, match="checksum"):
        archive.read_account(data.replace(b'"Main"', b'"Evil"'))
    with pytest.raises(ArchiveError, match="not a PaperDesk account export"):
        archive.read_account(b'{"format": "something"}')
    with pytest.raises(ArchiveError, match="not valid JSON"):
        archive.read_account(b"\x00\x01")
    d = json.loads(data)
    d["format_version"] = 99
    with pytest.raises(ArchiveError, match="newer PaperDesk"):
        archive.read_account(json.dumps(d).encode())
