"""Access control: optional passcode sign-in (LAN mode) and request hardening for every mode.

Settings come from the environment (and, when PAPERDESK_ENV_FILE names a file, from that .env file
for the access keys below; real environment variables win):

  PAPERDESK_LAN=1               let devices on the network connect; requires PAPERDESK_PASSCODE
  PAPERDESK_PASSCODE=...        passcode, at least 8 characters; when set, the API needs a session
  PAPERDESK_SESSION_DAYS=30     sign-ins expire after this many days without use
  PAPERDESK_LOOPBACK_BYPASS=1   requests from this computer (127.0.0.1, ::1) skip the passcode
  PAPERDESK_ALLOWED_HOSTS=...   extra host names the server answers to (for example mypc.local)
  PAPERDESK_BIND=127.0.0.1      Docker only: the host address compose publishes the port on

What is enforced, in every mode:
  * Host header check (blunts DNS rebinding): with LAN mode off only localhost, 127.0.0.0/8 and ::1;
    in LAN mode also any IP address literal and PAPERDESK_ALLOWED_HOSTS.
  * Cross-origin check on every non-GET request and on the WebSocket handshake.
  * Security headers on every HTTP response.
With a passcode set, every /api route (except the sign-in endpoints), /ws and the API docs need a
session; the static UI shell stays public so it can show the sign-in screen.

Sessions are random tokens. Only their SHA-256 digests are kept, in memory and, when the exchange has a
data directory, in <data>/sessions.json (created with mode 0600). The file also holds a salted scrypt
check value of the passcode so that changing the passcode signs every device out.

Time here is wall-clock time (session expiry and login backoff are about real elapsed time, not the
simulated market), injected as ``clock`` so tests can control it.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

log = logging.getLogger("paperdesk.access")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("paperdesk access: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

COOKIE = "paperdesk_session"
MIN_PASSCODE = 8
MAX_PASSCODE = 1024
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_API = {"/api/auth/login", "/api/auth/logout", "/api/auth/state"}
DOCS = ("/docs", "/redoc", "/openapi.json")
ACCESS_KEYS = ("PAPERDESK_LAN", "PAPERDESK_PASSCODE", "PAPERDESK_SESSION_DAYS", "PAPERDESK_LOOPBACK_BYPASS",
               "PAPERDESK_ALLOWED_HOSTS", "PAPERDESK_BIND")
_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"", "0", "false", "no", "off"}
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


class ConfigError(RuntimeError):
    """The access settings are unsafe or invalid; the server must not start."""


# ---- configuration ---------------------------------------------------------------------------

def read_env_file(path: str) -> dict:
    """Parse KEY=VALUE lines like docker compose does for the simple cases: '#' comments, optional
    'export ', single or double quotes (taken literally), and ' #' inline comments on unquoted values."""
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            out[key] = value
    return out


def _flag(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False if v else default
    raise ConfigError(f"{key} must be 1 (on) or 0 (off), not {raw!r}.")


def _is_loopback_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


@dataclass(frozen=True)
class Config:
    lan: bool = False
    passcode: Optional[str] = field(default=None, repr=False)
    session_days: float = 30.0
    loopback_bypass: bool = True
    allowed_hosts: tuple = ()

    @property
    def auth(self) -> bool:
        return self.passcode is not None


def load_config(environ: Optional[dict] = None) -> Config:
    env = dict(os.environ if environ is None else environ)
    path = env.get("PAPERDESK_ENV_FILE")
    if path and os.path.isfile(path):
        try:
            for k, v in read_env_file(path).items():
                if k in ACCESS_KEYS and k not in env:
                    env[k] = v
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"Could not read the settings file {path}: {e}.")
    lan = _flag(env, "PAPERDESK_LAN", False)
    passcode = env.get("PAPERDESK_PASSCODE") or None
    if passcode is not None and len(passcode) < MIN_PASSCODE:
        raise ConfigError(f"PAPERDESK_PASSCODE is too short: use at least {MIN_PASSCODE} characters. "
                          "PaperDesk did not start.")
    if passcode is not None and len(passcode) > MAX_PASSCODE:
        raise ConfigError(f"PAPERDESK_PASSCODE is too long: use at most {MAX_PASSCODE} characters.")
    if lan and passcode is None:
        raise ConfigError("LAN mode (PAPERDESK_LAN=1) needs a passcode: set PAPERDESK_PASSCODE to at least "
                          f"{MIN_PASSCODE} characters in .env or the environment. PaperDesk did not start.")
    bind = (env.get("PAPERDESK_BIND") or "").strip()
    if bind and not _is_loopback_host(bind) and not lan:
        raise ConfigError(f"PAPERDESK_BIND={bind} publishes PaperDesk to your network, but LAN mode is off, so "
                          "anyone on the network could use it without a passcode. Set PAPERDESK_LAN=1 and "
                          "PAPERDESK_PASSCODE, or set PAPERDESK_BIND=127.0.0.1. PaperDesk did not start.")
    raw_days = env.get("PAPERDESK_SESSION_DAYS") or "30"
    try:
        days = float(raw_days)
    except ValueError:
        days = -1.0
    if not 0 < days <= 3650:
        raise ConfigError(f"PAPERDESK_SESSION_DAYS must be a number of days between 0 and 3650, not {raw_days!r}.")
    hosts = []
    for h in (env.get("PAPERDESK_ALLOWED_HOSTS") or "").split(","):
        h = h.strip().lower().rstrip(".")
        if not h:
            continue
        if not _HOSTNAME.match(h):
            raise ConfigError(f"PAPERDESK_ALLOWED_HOSTS contains {h!r}, which is not a plain host name "
                              "(no wildcards, ports or schemes).")
        hosts.append(h)
    return Config(lan=lan, passcode=passcode, session_days=days,
                  loopback_bypass=_flag(env, "PAPERDESK_LOOPBACK_BYPASS", True), allowed_hosts=tuple(hosts))


# ---- client addresses --------------------------------------------------------------------------

def _ip(host: Optional[str]):
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_loopback_client(host: Optional[str]) -> bool:
    ip = _ip(host)
    return bool(ip and ip.is_loopback)


def client_key(host: Optional[str]) -> str:
    """Rate-limit bucket for a client: the IPv4 address, or the /64 network of an IPv6 address
    (one device can use many addresses inside its /64)."""
    ip = _ip(host)
    if ip is None:
        return host or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


# ---- sign-in attempt limiting -------------------------------------------------------------------

class Limiter:
    """Per-client exponential backoff plus a global cap on failed sign-ins.

    The first FREE failures from a client cost nothing (typos). After that each failure makes the client
    wait 1, 2, 4, ... seconds (at most MAX_DELAY) before the next attempt is even checked. Independently,
    once GLOBAL_MAX failures from all clients together fall inside WINDOW seconds, every sign-in is refused
    until the window has moved on. A client's record is forgotten FORGET seconds after its last failure.
    """
    FREE = 3
    MAX_DELAY = 900.0
    GLOBAL_MAX = 30
    WINDOW = 900.0
    FORGET = 86400.0
    MAX_CLIENTS = 4096

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.clients: "collections.OrderedDict[str, list]" = collections.OrderedDict()
        self.recent: collections.deque = collections.deque()

    def _delay(self, failures: int) -> float:
        return 0.0 if failures < self.FREE else min(2.0 ** (failures - self.FREE), self.MAX_DELAY)

    def retry_after(self, key: str) -> float:
        now = self.clock()
        while self.recent and now - self.recent[0] >= self.WINDOW:
            self.recent.popleft()
        if len(self.recent) >= self.GLOBAL_MAX:
            return self.recent[0] + self.WINDOW - now
        rec = self.clients.get(key)
        if rec and now - rec[1] >= self.FORGET:
            del self.clients[key]
            rec = None
        if rec:
            wait = rec[1] + self._delay(rec[0]) - now
            if wait > 0:
                return wait
        return 0.0

    def failed(self, key: str) -> tuple:
        """Record a failure; returns (failures from this client, seconds until it may try again)."""
        now = self.clock()
        rec = self.clients.pop(key, None) or [0, now]
        rec[0] += 1
        rec[1] = now
        self.clients[key] = rec
        while len(self.clients) > self.MAX_CLIENTS:
            self.clients.popitem(last=False)
        self.recent.append(now)
        return rec[0], self._delay(rec[0])

    def succeeded(self, key: str) -> None:
        self.clients.pop(key, None)


# ---- sessions --------------------------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _passcode_check(passcode: str, salt: bytes) -> str:
    return hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


class Sessions:
    """Server-side sessions keyed by the SHA-256 of a random token. Idle sessions expire."""
    MAX = 100
    SAVE_SEEN_EVERY = 3600.0      # last-use times are written to disk at most this often
    REFRESH_COOKIE_EVERY = 86400.0

    def __init__(self, idle_seconds: float, clock: Callable[[], float], path: Optional[str] = None,
                 passcode: Optional[str] = None):
        self.idle = idle_seconds
        self.clock = clock
        self.path = path
        self.items: dict = {}
        self.lock = threading.Lock()
        self._check: Optional[dict] = None
        if path and passcode is not None:
            self._load(passcode)

    # persistence
    def _load(self, passcode: str) -> None:
        data = None
        try:
            if os.name == "posix" and os.path.exists(self.path) and os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)
                log.warning("tightened the permissions of %s to owner-only", self.path)
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); everyone has to sign in again", self.path, type(e).__name__)
        check = data.get("passcode_check") if isinstance(data, dict) else None
        if isinstance(check, dict) and isinstance(check.get("salt"), str):
            try:
                salt = bytes.fromhex(check["salt"])
            except ValueError:
                salt = b""
            if salt and hmac.compare_digest(_passcode_check(passcode, salt), str(check.get("value", ""))):
                self._check = check
                now = self.clock()
                for h, rec in (data.get("sessions") or {}).items():
                    if (isinstance(rec, dict) and re.fullmatch(r"[0-9a-f]{64}", h)
                            and isinstance(rec.get("seen"), (int, float)) and now - rec["seen"] < self.idle):
                        self.items[h] = {"created": float(rec.get("created", rec["seen"])), "seen": float(rec["seen"]),
                                         "saved_seen": float(rec["seen"]), "cookie": float(rec["seen"])}
            else:
                log.info("the passcode changed since the last start, so every device has to sign in again")
        if self._check is None:
            salt = secrets.token_bytes(16)
            self._check = {"salt": salt.hex(), "value": _passcode_check(passcode, salt)}
        self._save()

    def _save(self) -> None:
        if not self.path:
            return
        data = {"version": 1, "passcode_check": self._check,
                "sessions": {h: {"created": r["created"], "seen": r["seen"]} for h, r in self.items.items()}}
        d = os.path.dirname(self.path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".sessions.", suffix=".tmp")  # mkstemp creates it 0600
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            for r in self.items.values():
                r["saved_seen"] = r["seen"]
        except OSError as e:
            log.warning("could not save sign-ins to %s (%s); they will not survive a restart",
                        self.path, type(e).__name__)

    # operations
    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            self._purge(now)
            while len(self.items) >= self.MAX:
                oldest = min(self.items, key=lambda h: self.items[h]["seen"])
                del self.items[oldest]
            self.items[_digest(token)] = {"created": now, "seen": now, "saved_seen": now, "cookie": now}
            self._save()
        return token

    def _purge(self, now: float) -> bool:
        dead = [h for h, r in self.items.items() if now - r["seen"] >= self.idle]
        for h in dead:
            del self.items[h]
        return bool(dead)

    def check(self, token: Optional[str], touch: bool = True) -> bool:
        """True when the token names a live session. With touch, counts as use (extends the idle timer)."""
        if not token or len(token) > 200:
            return False
        h = _digest(token)
        now = self.clock()
        with self.lock:
            rec = self.items.get(h)
            if rec is None:
                return False
            if now - rec["seen"] >= self.idle:
                del self.items[h]
                self._save()
                return False
            if touch:
                rec["seen"] = now
                if now - rec["saved_seen"] >= self.SAVE_SEEN_EVERY:
                    self._purge(now)
                    self._save()
            return True

    def needs_cookie_refresh(self, token: str) -> bool:
        with self.lock:
            rec = self.items.get(_digest(token))
            if rec and self.clock() - rec["cookie"] >= self.REFRESH_COOKIE_EVERY:
                rec["cookie"] = self.clock()
                return True
        return False

    def revoke(self, token: Optional[str]) -> None:
        if not token:
            return
        with self.lock:
            if self.items.pop(_digest(token), None) is not None:
                self._save()


# ---- the access policy -----------------------------------------------------------------------------

def split_host(value: str) -> tuple:
    """'Example.com:8000' -> ('example.com', '8000'); '[::1]:80' -> ('::1', '80'). ('', '') when malformed."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return "", ""
        host, rest = value[1:end], value[end + 1:]
        if rest and not rest.startswith(":"):
            return "", ""
        port = rest[1:]
        if not re.fullmatch(r"[0-9a-f:.]+", host) or _ip(host) is None:  # no zone ids: the value goes into CSP
            return "", ""
    else:
        host, _, port = value.partition(":")
        host = host.rstrip(".")
        if not host or not (_HOSTNAME.match(host) or _ip(host) is not None):
            return "", ""
    if port and not (port.isdigit() and len(port) <= 5):
        return "", ""
    return host, port


class Access:
    def __init__(self, config: Config, data_dir: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.config = config
        self._clock = clock
        self.limiter = Limiter(self.now)
        path = os.path.join(data_dir, "sessions.json") if data_dir and config.auth else None
        self.sessions = Sessions(config.session_days * 86400.0, self.now, path, config.passcode)

    # Tests replace ``clock`` to move time; everything reads it through now().
    @property
    def clock(self):
        return self._clock

    @clock.setter
    def clock(self, fn):
        self._clock = fn

    def now(self) -> float:
        return self._clock()

    def check_passcode(self, given: str) -> bool:
        """Constant-time comparison (digests first, so the length is not revealed either)."""
        expected = self.config.passcode
        if expected is None or not isinstance(given, str):
            return False
        return hmac.compare_digest(hashlib.sha256(given.encode("utf-8")).digest(),
                                   hashlib.sha256(expected.encode("utf-8")).digest())

    def host_ok(self, host_header: str, scope: dict) -> bool:
        host, _ = split_host(host_header)
        if not host:
            return False
        if host == "testserver" and (scope.get("server") or ("",))[0] == "testserver":
            return True  # Starlette's in-process TestClient; a real server never reports this name
        if host in LOOPBACK_NAMES or _is_loopback_host(host) or host in self.config.allowed_hosts:
            return True
        # An IP literal cannot be rebound by an attacker's DNS, so in LAN mode any address of this machine works.
        return self.config.lan and _ip(host) is not None

    @staticmethod
    def origin_ok(headers, host_header: str, scheme: str = "http") -> bool:
        """Reject requests a browser marks as coming from another site or origin. Clients that send no
        Origin (curl, scripts) are not browsers and cannot carry a victim's cookies, so they pass."""
        site = headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return False
        origin = headers.get("origin")
        if origin is None:
            return True
        try:
            o = urlsplit(origin)
            o_port = o.port
        except ValueError:
            return False
        if o.scheme != scheme or not o.hostname or o.path not in ("", "/"):
            return False
        host, port = split_host(host_header)
        if not host or o.hostname.lower().rstrip(".") != host:
            return False
        if port:
            return (o_port or (443 if o.scheme == "https" else 80)) == int(port)
        return o_port is None

    def protected(self, path: str) -> bool:
        if not self.config.auth:
            return False
        if path in PUBLIC_API:
            return False
        if path == "/api" or path.startswith("/api/") or path == "/ws" or path.startswith("/ws/"):
            return True
        return any(path == d or path.startswith(d + "/") for d in DOCS)

    def bypassed(self, scope: dict) -> bool:
        return self.config.loopback_bypass and is_loopback_client((scope.get("client") or ("",))[0])


def cookie_header(token: str, max_age: int, secure: bool) -> str:
    parts = [f"{COOKIE}={token}", f"Max-Age={max_age}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _csp(host_header: str) -> str:
    host = host_header.strip().lower()
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            f"font-src 'self' data:; connect-src 'self' ws://{host} wss://{host}; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class AccessMiddleware:
    """Pure ASGI middleware, so it guards HTTP and the WebSocket handshake alike."""

    def __init__(self, app, access: Access):
        self.app = app
        self.access = access

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.requests import HTTPConnection
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        path = scope.get("path", "")
        ws = scope["type"] == "websocket"
        acc = self.access

        if not acc.host_ok(host, scope):
            return await self._refuse(scope, receive, send, 421,
                                      "This PaperDesk server does not answer to that host name. Open it with the "
                                      "address the server printed when it started.")
        if (ws or scope.get("method", "GET") not in SAFE_METHODS) and not acc.origin_ok(
                headers, host, "https" if scope.get("scheme") in ("https", "wss") else "http"):
            return await self._refuse(scope, receive, send, 403,
                                      "Refused a request that came from another website.")
        token = None
        if acc.protected(path) and not acc.bypassed(scope):
            token = HTTPConnection(scope).cookies.get(COOKIE)
            if not acc.sessions.check(token):
                return await self._refuse(scope, receive, send, 401,
                                          "Sign in required: this PaperDesk server needs its passcode.")

        if ws:
            if token:
                send = self._watch_session(send, token)
            return await self.app(scope, receive, send)

        secure = scope.get("scheme") == "https"
        refresh = bool(token) and acc.sessions.needs_cookie_refresh(token)
        is_api = path.startswith("/api")
        is_docs = any(path == d or path.startswith(d + "/") for d in DOCS)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                have = {k.lower() for k, _ in raw}

                def add(name: str, value: str):
                    if name.encode() not in have:
                        raw.append((name.encode(), value.encode("latin-1")))
                add("x-content-type-options", "nosniff")
                add("referrer-policy", "no-referrer")
                add("x-frame-options", "DENY")
                add("cross-origin-opener-policy", "same-origin")
                if not is_docs:
                    add("content-security-policy", _csp(host))
                if is_api:
                    add("cache-control", "no-store")
                if refresh:
                    raw.append((b"set-cookie", cookie_header(token, int(acc.sessions.idle), secure).encode()))
                message = {**message, "headers": raw}
            await send(message)

        return await self.app(scope, receive, send_with_headers)

    def _watch_session(self, send, token: str):
        """Close an open stream (code 4401) as soon as its session ends: sign-out or expiry."""
        sessions = self.access.sessions
        state = {"closed": False}

        async def watched(message):
            if message["type"] == "websocket.send" and not state["closed"] and not sessions.check(token, touch=False):
                state["closed"] = True
                await send({"type": "websocket.close", "code": 4401, "reason": "Signed out"})
                raise ConnectionError("session ended")
            if state["closed"] and message["type"] == "websocket.send":
                raise ConnectionError("session ended")
            await send(message)
        return watched

    async def _refuse(self, scope, receive, send, status: int, detail: str):
        if scope["type"] == "websocket":
            # Closing before accepting makes the server answer the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 1008})
            return
        from starlette.responses import JSONResponse
        body = {"detail": detail}
        if status == 401:
            body["auth_required"] = True
        resp = JSONResponse(body, status_code=status, headers={
            "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY",
            "cache-control": "no-store", "content-security-policy": "default-src 'none'; frame-ancestors 'none'"})
        await resp(scope, receive, send)


# ---- start-up helper for run.sh / run.bat ------------------------------------------------------------

def lan_addresses() -> list:
    """This machine's private IPv4 addresses. Sends no packets (a UDP connect only picks a route)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: never contacted
        found.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        if s:
            s.close()
    out = []
    for a in found:
        ip = _ip(a)
        if ip and ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local:
            out.append(a)
    return sorted(out, key=lambda a: tuple(int(x) for x in a.split(".")))


def preflight(environ: Optional[dict] = None, out=None, err=None) -> int:
    """Validate the access settings and choose the bind address. Prints the address on stdout (only that,
    so the scripts can capture it) and everything for people on stderr. Returns the exit code."""
    env = os.environ if environ is None else environ
    out, err = out or sys.stdout, err or sys.stderr
    try:
        cfg = load_config(env)
    except ConfigError as e:
        print(str(e), file=err)
        return 1
    host = (env.get("HOST") or "").strip()
    port = (env.get("PORT") or "8000").strip()
    if not cfg.lan:
        host = host or "127.0.0.1"
        if not _is_loopback_host(host):
            print(f"HOST={host} would let other devices reach PaperDesk without a passcode. To use PaperDesk from "
                  "a phone, set PAPERDESK_LAN=1 and PAPERDESK_PASSCODE (see README.md); otherwise leave HOST unset.",
                  file=err)
            return 1
        if cfg.auth:
            print("A passcode is set: browsers must sign in (this computer skips it unless "
                  "PAPERDESK_LOOPBACK_BYPASS=0).", file=err)
        print(host, file=out)
        return 0
    host = host or "0.0.0.0"
    addrs = lan_addresses() if host in ("0.0.0.0", "::") else [host]
    print("", file=err)
    print("LAN mode is on: devices on your network can open PaperDesk after entering the passcode.", file=err)
    if addrs:
        print("On your phone, open " + ("one of these addresses:" if len(addrs) > 1 else "this address:"), file=err)
        for a in addrs:
            print(f"    http://{a}:{port}", file=err)
    else:
        print("Could not find this computer's network address. Look it up with 'ipconfig' (Windows) or "
              f"'ip addr' (Linux, macOS) and open http://<that address>:{port} on your phone.", file=err)
    print("The phone must be on the same Wi-Fi network as this computer.", file=err)
    print("Windows may ask whether to allow Python through the firewall: allow it on Private networks only, "
          "never on Public networks.", file=err)
    print("This is plain HTTP: anyone on the same network could watch the traffic. Do not use LAN mode on "
          "public or shared Wi-Fi, and do not reuse this passcode anywhere else.", file=err)
    print("", file=err)
    print(host, file=out)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["preflight"]:
        print("usage: python -m app.api.auth preflight", file=sys.stderr)
        sys.exit(2)
    sys.exit(preflight())
