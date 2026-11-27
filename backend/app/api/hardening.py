"""Hardening helpers that PR 30 left out: WebSocket message checks and connection caps, a per-client rate
limit for provider-backed GETs, and data-directory permissions. docs/LIMITS.md describes each limit."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# ---- limits -----------------------------------------------------------------------------------------------
MAX_SYMBOL_TEXT = 32              # as the stream already cut symbol names to
MAX_WS_MESSAGE = 16 * 1024        # characters in one message from a browser
MAX_WS_SYMBOLS = 300              # subscribed symbols per connection (as before)
MAX_WS_CONNECTIONS = 32           # open streams in total
MAX_WS_PER_ADDRESS = 8            # open streams from one client address
WS_SEND_TIMEOUT = 5.0             # seconds a client may take to accept one tick message
WS_CLOSE_POLICY = 1008            # close code: the client broke a rule
WS_CLOSE_TOO_BIG = 1009           # close code: message too large
RESEARCH_RATE_PER_MINUTE = 60     # provider-backed research requests per client per minute
RESEARCH_RATE_BURST = 20


# ---- WebSocket messages -----------------------------------------------------------------------------------

class BadMessage(ValueError):
    def __init__(self, message: str, code: int = WS_CLOSE_POLICY):
        super().__init__(message)
        self.code = code


def ws_update(text: Optional[str], sub: dict) -> None:
    """Apply one message from a browser to its subscription, or raise BadMessage to close the stream.

    The stream closes for a binary frame, a message over MAX_WS_MESSAGE characters, text that is not JSON,
    or JSON that is not an object. Inside an object it stays tolerant, as before: "symbols" (a list; the
    first MAX_WS_SYMBOLS are kept, each cut to MAX_SYMBOL_TEXT characters) and "account" (an id string;
    any other value means no account).
    """
    import json
    if text is None:
        raise BadMessage("send text messages only", 1003)
    if len(text) > MAX_WS_MESSAGE:
        raise BadMessage("message too large", WS_CLOSE_TOO_BIG)
    try:
        msg = json.loads(text)
    except (ValueError, RecursionError):
        raise BadMessage("message is not JSON") from None
    if not isinstance(msg, dict):
        raise BadMessage("message must be a JSON object")
    if isinstance(msg.get("symbols"), list):
        sub["symbols"] = [str(s).upper()[:MAX_SYMBOL_TEXT] for s in msg["symbols"][:MAX_WS_SYMBOLS]]
    if "account" in msg:  # an id string; anything else would break the broadcast for everyone
        acct = msg["account"]
        sub["account"] = acct if isinstance(acct, str) and len(acct) <= MAX_SYMBOL_TEXT else None


# ---- per-client rate limit ----------------------------------------------------------------------------------

class ClientRate:
    """Token bucket per client address, for GETs that spend a provider's request budget."""

    MAX_CLIENTS = 256

    def __init__(self, per_minute: float = RESEARCH_RATE_PER_MINUTE, burst: float = RESEARCH_RATE_BURST,
                 clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = per_minute / 60.0, float(burst), clock
        self.buckets: dict = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> float:
        """0 when allowed (and a token is taken); otherwise seconds until the next request is allowed."""
        now = self.clock()
        with self.lock:
            tokens, last = self.buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            wait = 0.0 if tokens >= 1 else (1 - tokens) / self.rate
            if not wait:
                tokens -= 1
            self.buckets[key] = (tokens, now)  # re-inserted: the dict is in least-recently-used order
            while len(self.buckets) > self.MAX_CLIENTS:
                self.buckets.pop(next(iter(self.buckets)))
            return wait


# ---- file permissions -------------------------------------------------------------------------------------

def private_dir(path: str) -> None:
    """Create the data directory readable by this user only (POSIX 0700). An existing one is left alone."""
    import os
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)


def permission_warnings(paths: list) -> list:
    """Plain warnings for files or directories other users of this computer can read (POSIX only).

    The data directory holds account state and sign-in digests; .env can hold the passcode and provider
    keys. On Windows, files under your user profile are private to your account by default, so nothing
    is checked there.
    """
    import os
    import stat
    if os.name != "posix":
        return []
    out = []
    for p in paths:
        try:
            mode = os.stat(p).st_mode
        except OSError:
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            fix = "chmod 700" if stat.S_ISDIR(mode) else "chmod 600"
            out.append(f"{p} can be read by other users of this computer (mode {stat.S_IMODE(mode):o}); run: {fix} {p}")
    return out
