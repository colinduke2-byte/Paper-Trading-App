"""Historical replay API. While a replay runs, ctx.ex is the replay sandbox, so every other endpoint
(quotes, charts, orders, accounts) works on the replayed market without changes."""
import os

from fastapi import Body, HTTPException

from ..core.money import parse as parse_num
from ..marketdata.replay.control import ReplayController
from ..marketdata.replay.format import SessionFormatError
from ..marketdata.replay.library import find_session, list_sessions, session_dirs


def register(app, ctx) -> None:
    def dirs() -> list:
        return session_dirs(ctx.live.data_dir)

    def active() -> ReplayController:
        rp = ctx.replay
        if rp is None:
            raise HTTPException(409, "No replay is running. Start one from the session list first.")
        return rp

    def state() -> dict:
        rp = ctx.replay
        return rp.status() if rp is not None else {"active": False}

    @app.get("/api/replay/sessions")
    def sessions():
        return list_sessions(dirs())

    @app.get("/api/replay/status")
    def replay_status():
        return state()

    @app.post("/api/replay/start")
    def start(body: dict = Body(...)):
        if ctx.replay is not None:
            raise HTTPException(409, "A replay is already running. Exit it before starting another.")
        path = find_session(dirs(), str(body.get("session", "")))
        cash = parse_num(body.get("starting_cash", 100000), "starting cash")
        if cash <= 0:
            raise ValueError("starting cash must be positive")
        sandbox = os.path.join(ctx.live.data_dir, "replay") if ctx.live.data_dir else None
        try:
            controller = ReplayController(path, sandbox, starting_cash=cash,
                                          account_type=str(body.get("account_type", "margin")),
                                          speed=body.get("speed", 1))
        except SessionFormatError as e:
            raise HTTPException(422, f"The session file cannot be replayed: {e}")
        try:
            ctx.enter_replay(controller)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        if body.get("play"):
            controller.play()
        return state()

    @app.post("/api/replay/pause")
    def pause():
        active().pause()
        return state()

    @app.post("/api/replay/resume")
    def resume():
        active().play()
        return state()

    @app.post("/api/replay/speed")
    def speed(body: dict = Body(...)):
        active().set_speed(body.get("speed"))
        return state()

    @app.post("/api/replay/step")
    def step(body: dict = Body(default={})):
        active().step(body.get("seconds", 60))
        return state()

    @app.post("/api/replay/seek")
    def seek(body: dict = Body(...)):
        rp = active()
        if "time" in body:
            rp.seek(body["time"])
        elif "offset" in body:  # seconds from the session start
            try:
                rp.seek(rp.start + float(body["offset"]))
            except (TypeError, ValueError):
                raise ValueError("offset must be a number of seconds")
        else:
            raise ValueError("give 'time' (epoch seconds) or 'offset' (seconds from the session start)")
        return state()

    @app.post("/api/replay/exit")
    def exit_replay():
        active()
        try:
            ctx.exit_replay()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return state()
