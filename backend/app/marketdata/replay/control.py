"""A replay run: its own simulated clock, replay feed, sandbox exchange and data directory.

The server's tick loop calls advance(wall_seconds); the API calls play, pause, step, seek and
set_speed. The clock moves in whole simulated seconds and the exchange is ticked once for each one,
so fills, day-order expiry, option expiry and session changes happen at the simulated time, and the
same session with the same commands at the same simulated times always gives the same ledger.
"""
import math
import os
import shutil
import threading
from decimal import Decimal
from itertools import count
from typing import Optional

from ...core import calendar as cal
from ...core.clock import SimClock
from .feed import ReplayFeed
from .format import read_session, session_id

MIN_SPEED, MAX_SPEED = 1.0, 60.0
MAX_TICKS_PER_ADVANCE = 240  # bounded work per loop turn; a slow machine falls behind instead of freezing
MAX_SEEK_SECONDS = 7 * 86400
SANDBOX_ACCOUNT_START = 9001  # replay account ids (A9001...) never look like live ones


class ReplayController:
    def __init__(self, session_path: str, data_dir: Optional[str] = None, starting_cash=Decimal("100000"),
                 account_type: str = "margin", speed: float = 1.0, account_name: str = "Replay account"):
        from ...exchange import Exchange  # imported here: marketdata must not depend on the exchange at import time

        if account_type not in ("margin", "cash"):
            raise ValueError("account type must be 'margin' or 'cash'")
        self.session_path = session_path
        self.session_id = session_id(session_path)
        data = read_session(session_path)
        self.header = data.header
        self.feed = ReplayFeed(data)
        self.clock = SimClock(self.feed.start)
        if data_dir:
            # The sandbox gets a fresh directory of its own; it never shares files with live state.
            if os.path.isdir(data_dir):
                shutil.rmtree(data_dir)
            os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.ex = Exchange(self.feed, self.clock, data_dir=data_dir, session_mode="us")
        self.ex._acct_seq = count(SANDBOX_ACCOUNT_START)
        self.ex.create_account(account_name, account_type, starting_cash)
        self.ex.watchlists = {"Main": list(self.feed.symbols())}
        self.speed = 1.0
        self.set_speed(speed)
        self.paused = True
        self._carry = 0.0
        self._lock = threading.RLock()
        self.ticks = 0
        self.jumps = 0  # step and seek commands; lets the UI reload charts after a jump
        self.lagging = False
        self._tick()  # release the opening events at the start time

    # ---- clock -----------------------------------------------------------
    @property
    def start(self) -> float:
        return self.feed.start

    @property
    def end(self) -> float:
        return self.feed.end

    def now(self) -> float:
        return self.clock.now()

    @property
    def ended(self) -> bool:
        return self.now() >= self.end

    def _tick(self) -> None:
        self.ex.tick()
        self.ticks += 1

    def _run(self, seconds: int) -> int:
        """Move the clock forward `seconds` whole seconds, ticking the exchange at each one."""
        n = max(0, min(int(seconds), math.ceil(self.end - self.now())))
        for _ in range(n):
            self.clock.set(min(self.now() + 1, self.end))
            self._tick()
        if self.ended:
            self.paused = True
            self._carry = 0.0
        return n

    def advance(self, wall_seconds: float) -> int:
        """Called by the tick loop with the wall time since its last turn. Never blocks."""
        if self.paused or self.ended or wall_seconds <= 0:
            return 0
        if not self._lock.acquire(blocking=False):
            return 0  # a step or seek is running; it moves the clock itself
        try:
            self._carry += self.speed * wall_seconds
            n = int(self._carry)
            self._carry -= n
            self.lagging = n > MAX_TICKS_PER_ADVANCE
            if self.lagging:
                n, self._carry = MAX_TICKS_PER_ADVANCE, 0.0
            return self._run(n)
        finally:
            self._lock.release()

    # ---- commands ----------------------------------------------------------
    def play(self) -> None:
        if self.ended:
            raise ValueError("the session has ended; exit the replay or start it again")
        self.paused = False
        self._carry = 0.0

    def pause(self) -> None:
        self.paused = True
        self._carry = 0.0

    def set_speed(self, speed) -> None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise ValueError("speed must be a number from 1 to 60") from None
        if not (MIN_SPEED <= speed <= MAX_SPEED) or math.isnan(speed):
            raise ValueError("speed must be from 1x to 60x")
        self.speed = speed

    def step(self, seconds=60) -> int:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            raise ValueError("step must be a whole number of seconds") from None
        if not 1 <= seconds <= 3600:
            raise ValueError("step must be from 1 to 3600 seconds")
        if self.ended:
            raise ValueError("the session has ended")
        with self._lock:
            self.jumps += 1
            return self._run(seconds)

    def seek(self, ts) -> int:
        """Jump forward to simulated time `ts`. Orders work through every second on the way."""
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            raise ValueError("time must be epoch seconds") from None
        if math.isnan(ts):
            raise ValueError("time must be epoch seconds")
        with self._lock:
            if ts < self.now():
                raise ValueError("a replay cannot go back in time; exit and start the session again to see earlier")
            ts = min(ts, self.end)
            seconds = math.ceil(ts - self.now())
            if seconds > MAX_SEEK_SECONDS:
                raise ValueError("that time is too far ahead")
            self.jumps += 1
            return self._run(seconds)

    def close(self) -> None:
        self.paused = True
        try:
            self.ex.save(force=True)
        except OSError:
            pass

    # ---- reporting -----------------------------------------------------------
    def status(self) -> dict:
        now = self.now()
        h = self.header
        return {"active": True, "session_id": self.session_id, "label": h.get("label"), "date": h["date"],
                "description": h.get("description", ""), "source": h.get("source", ""),
                "simulated": self.feed.simulated, "time": now, "start": self.start, "end": self.end,
                "progress": self.feed.progress(now)["fraction"], "speed": self.speed, "paused": self.paused,
                "ended": self.ended, "lagging": self.lagging, "jumps": self.jumps, "session": cal.session(now),
                "run_id": f"{self.session_id}@{id(self):x}"}
