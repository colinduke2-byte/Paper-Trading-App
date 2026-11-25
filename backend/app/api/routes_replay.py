"""Historical replay API. While a replay runs, ctx.ex is the replay sandbox, so every other endpoint
(quotes, charts, orders, accounts) works on the replayed market without changes."""
import os
import threading

from fastapi import Body, HTTPException

from ..core.money import parse as parse_num
from ..marketdata.replay.control import ReplayController
from ..marketdata.replay.format import SessionFormatError
from ..marketdata.replay.library import find_session, list_sessions, session_dirs
from .locking import unlocked

# These routes take the exchange lock themselves (ctx.enter_replay, ctx.exit_replay, and the controller
# in short slices during a step or seek), because they also read session files or run for a long time.
SLOW = "reads session files or runs many ticks; takes the exchange lock itself in short pieces"


def register(app, ctx) -> None:
    starting = threading.Lock()  # one start at a time: two would build their sandbox in the same directory

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
    @unlocked(SLOW)
    def sessions():
        return list_sessions(dirs())

    @app.get("/api/replay/status")
    def replay_status():
        return state()

    @app.post("/api/replay/start")
    @unlocked(SLOW)
    def start(body: dict = Body(...)):
        if not starting.acquire(blocking=False):
            raise HTTPException(409, "A replay is already being started.")
        try:
            return _start(body)
        finally:
            starting.release()

    def _start(body: dict):
        if ctx.replay is not None:
            raise HTTPException(409, "A replay is already running. Exit it before starting another.")
        path = find_session(dirs(), str(body.get("session", "")))
        cash = parse_num(body.get("starting_cash", 100000))
        if cash <= 0:
            raise ValueError("starting cash must be positive")
        sandbox = os.path.join(ctx.live.data_dir, "replay") if ctx.live.data_dir else None
        try:
            controller = ReplayController(path, sandbox, starting_cash=cash,
                                          account_type=str(body.get("account_type", "margin")),
                                          speed=body.get("speed", 1))
        except SessionFormatError as e:
            raise HTTPException(422, f"The session file cannot be replayed: {e}")
        if body.get("play"):
            controller.play()
        try:
            ctx.enter_replay(controller)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return controller.status()

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
    @unlocked(SLOW)
    def step(body: dict = Body(default={})):
        active().step(body.get("seconds", 60))
        return state()

    @app.post("/api/replay/seek")
    @unlocked(SLOW)
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
    @unlocked(SLOW)
    def exit_replay():
        active()
        try:
            ctx.exit_replay()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return state()
