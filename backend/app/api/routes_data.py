"""Data and backups: save status, backup list, download, restore with a dry run, account export and import,
and the recovery choices after a damaged or newer state file (docs/STATE_FORMAT.md).

GET    /api/data/status                     save health, recovery, backup settings and the newest backup
GET    /api/data/backups                    automatic and manual backups, newest first
POST   /api/data/backups                    take a manual backup now
GET    /api/data/backups/{name}             download one backup
DELETE /api/data/backups/{name}             delete one backup
GET    /api/data/export                     download a fresh archive of everything in this data mode
POST   /api/data/restore/preview            body: a backup archive (application/zip), or JSON {"backup": name};
                                            checks it and answers what would change, with a token
POST   /api/data/restore/apply              {"token"}: snapshot the current state, then restore
GET    /api/data/accounts/{aid}/export      download one account (JSON)
POST   /api/data/accounts/import/preview    body: an exported account (JSON); answers what would be added, with a token
POST   /api/data/accounts/import/apply      {"token", "name"?}
GET    /api/data/recovery                   what was found and the backups to choose from
POST   /api/data/recovery/restore           {"backup"?}: restore that backup (default: the newest good one)
POST   /api/data/recovery/fresh             start with new default accounts (the damaged files stay in recovery/)
POST   /api/data/recovery/quit              stop the server without changing anything

Every state-changing route is a POST or DELETE under /api/, so the access middleware applies its Origin check
and, with a passcode, the session check. Uploads are read with a hard size limit before anything is parsed.
"""
import json
import threading

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..storage import archive, fsutil
from ..storage.backups import BackupError
from ..storage.service import DataService

UPLOAD_TYPES = ("application/zip", "application/x-zip-compressed", "application/octet-stream")


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"The file is too large: at most {limit // (1024 * 1024)} MB is accepted.")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"The file is too large: at most {limit // (1024 * 1024)} MB is accepted.")
        chunks.append(chunk)
    return b"".join(chunks)


def _attachment(data: bytes, filename: str, media: str) -> Response:
    return Response(data, media_type=media, headers={
        "content-disposition": f'attachment; filename="{filename}"', "x-content-type-options": "nosniff"})


def register(app, ctx) -> None:
    svc = DataService(ctx)
    ctx.data = svc
    app.state.data = svc

    def no_replay():
        if ctx.replay is not None:
            raise HTTPException(409, "A replay is running. Exit the replay first: backups and restores work on your "
                                     "own accounts, which are put aside during a replay.")

    def need_backups():
        if svc.backups is None:
            raise HTTPException(409, "This server has no data folder, so it keeps no backups.")

    def bad(e: Exception, status: int = 422):
        raise HTTPException(status, str(e)[:1].upper() + str(e)[1:] + ("" if str(e).endswith(".") else "."))

    @app.get("/api/data/status")
    def data_status():
        return svc.status()

    @app.get("/api/data/backups")
    def list_backups():
        need_backups()
        return svc.backups.list()

    @app.post("/api/data/backups")
    def make_backup():
        need_backups()
        no_replay()
        try:
            return svc.snapshot("manual")
        except BackupError as e:
            bad(e, 409)

    @app.get("/api/data/backups/{name}")
    def download_backup(name: str):
        need_backups()
        path = svc.backups.path_of(name)  # KeyError -> 404 for anything that is not one of ours
        return _attachment(fsutil.read_bytes(path), name, "application/zip")

    @app.delete("/api/data/backups/{name}")
    def delete_backup(name: str):
        need_backups()
        svc.backups.delete(name)
        return {"ok": True}

    @app.get("/api/data/export")
    def export_all():
        no_replay()
        import os
        live = ctx.live
        with ctx.lock:
            st = live.store
            if st is not None and st.blocked:
                raise HTTPException(409, "The data folder is in recovery, so there is no current state to export. "
                                         "Download one of the backups instead.")
            doc, market, info = live.to_document(), (live.feed.snapshot() or None), svc.info(live)
        sessions = {}
        if live.data_dir:
            d = os.path.join(live.data_dir, "sessions")
            if os.path.isdir(d):
                for n in sorted(os.listdir(d)):
                    p = os.path.join(d, n)
                    if os.path.isfile(p) and archive._NAME.match(f"sessions/{n}"):
                        sessions[n] = fsutil.read_bytes(p)
        now = svc.wall()
        data = archive.build(doc, market, sessions, {"kind": "export", "created_ts": now, **info})
        return _attachment(data, f"paperdesk-backup-{fsutil.stamp(now)}.zip", "application/zip")

    @app.post("/api/data/restore/preview")
    async def restore_preview(request: Request):
        no_replay()
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            body = await _body(request, 4096)
            try:
                name = json.loads(body or b"{}").get("backup")
            except (ValueError, AttributeError):
                raise HTTPException(400, "Send {\"backup\": name} or a backup file.")
            need_backups()
            try:
                arc = svc.backups.read(str(name))
            except archive.ArchiveError as e:
                bad(e)
            source = f"backup {name}"
        elif ctype in UPLOAD_TYPES:
            data = await _body(request, archive.MAX_UPLOAD)
            try:
                arc = archive.read(data)
            except archive.ArchiveError as e:
                bad(e)
            source = "uploaded file"
        else:
            raise HTTPException(415, "Upload a PaperDesk backup file (.zip).")
        return svc.preview_restore(arc, source)

    @app.post("/api/data/restore/apply")
    async def restore_apply(request: Request):
        no_replay()
        body = await _json(request)
        try:
            return svc.apply_restore(str(body.get("token", "")))
        except LookupError as e:
            bad(e, 409)
        except (BackupError, RuntimeError) as e:
            bad(e, 409)

    @app.get("/api/data/accounts/{aid}/export")
    def export_account(aid: str):
        ex = ctx.ex
        with ctx.lock:
            a = ex.account(aid)
            now = svc.wall()
            data = archive.export_account(ex, aid, {"app_version": svc.info(ex)["app_version"], "created": fsutil.iso(now),
                                                    "feed": ex.feed.name, "simulated": bool(ex.feed.simulated)})
        safe = "".join(c if c.isalnum() else "-" for c in a.name)[:30].strip("-") or a.id
        return _attachment(data, f"paperdesk-account-{safe}-{fsutil.stamp(now)}.json", "application/json")

    @app.post("/api/data/accounts/import/preview")
    async def import_preview(request: Request):
        no_replay()
        data = await _body(request, archive.MAX_ACCOUNT_UPLOAD)
        try:
            raw = archive.read_account(data)
            return svc.preview_import(raw)
        except archive.ArchiveError as e:
            bad(e)

    @app.post("/api/data/accounts/import/apply")
    async def import_apply(request: Request):
        no_replay()
        body = await _json(request)
        try:
            return svc.apply_import(str(body.get("token", "")), body.get("name"))
        except LookupError as e:
            bad(e, 409)
        except (archive.ArchiveError, BackupError, RuntimeError) as e:
            bad(e, 409)

    # ---- recovery ----------------------------------------------------------------------------
    @app.get("/api/data/recovery")
    def recovery():
        return svc.recovery_options()

    @app.post("/api/data/recovery/restore")
    async def recovery_restore(request: Request):
        body = await _json(request)
        try:
            return svc.recover_restore(body.get("backup"))
        except LookupError as e:
            bad(e, 409)
        except archive.ArchiveError as e:
            bad(e)
        except (BackupError, RuntimeError) as e:
            bad(e, 409)

    @app.post("/api/data/recovery/fresh")
    def recovery_fresh():
        from .server import create_default_accounts
        try:
            return svc.recover_fresh(create_default_accounts)
        except RuntimeError as e:
            bad(e, 409)

    @app.post("/api/data/recovery/quit")
    def recovery_quit():
        st = ctx.live.store
        if st is None or not st.blocked:
            raise HTTPException(409, "Quit from here is only offered while the data folder is in recovery. "
                                     "Stop the server with Ctrl+C in its window.")
        threading.Timer(0.3, _shutdown).start()
        return {"ok": True, "detail": "PaperDesk is stopping. Nothing was changed on disk."}


async def _json(request: Request) -> dict:
    body = await _body(request, 4096)
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "The request body is not valid JSON.")
    if not isinstance(d, dict):
        raise HTTPException(400, "The request body must be a JSON object.")
    return d


def _shutdown() -> None:
    """Stop the server as Ctrl+C would; if that does not end the process soon, exit (nothing is unsaved:
    saving is off during recovery)."""
    import os
    import signal
    try:
        signal.raise_signal(signal.SIGINT)
    except (OSError, ValueError):
        pass
    t = threading.Timer(5.0, lambda: os._exit(0))
    t.daemon = True
    t.start()
