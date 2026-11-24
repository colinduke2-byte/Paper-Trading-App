"""Small HTTP helpers shared by the real data providers.

Only GET is implemented on purpose: provider code reads market data and never sends
orders anywhere. Network access goes through a Transport object so tests can replace
it with saved responses.

Hardening (docs/LIMITS.md): requests go to https addresses only, redirects are refused (the
credentials are in the headers, so a redirect to another host must never be followed), responses
are capped at limits.MAX_PROVIDER_RESPONSE and given an overall deadline, and numbers in provider
payloads are read tolerantly: an unreadable, infinite or absurd value becomes None and is counted
(invalid_count()), so one bad record can never raise into the tick.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from ..core import limits

MAX_PROVIDER_NUMBER = Decimal("1e15")  # any provider number larger than this is treated as invalid data
_invalid = {"count": 0, "last": None}
_invalid_lock = threading.Lock()


def _bad_value(v) -> None:
    with _invalid_lock:
        _invalid["count"] += 1
        _invalid["last"] = repr(v)[:40]


def invalid_count() -> int:
    """How many provider values were dropped as invalid since start-up."""
    return _invalid["count"]


def invalid_status() -> Optional[str]:
    """A user-facing note when the feed has sent invalid data, else None."""
    n = _invalid["count"]
    if not n:
        return None
    return (f"The data feed sent {n} invalid value{'s' if n != 1 else ''} (for example {_invalid['last']}); "
            "they were ignored.")


def safe_url(v) -> str:
    """A link from a provider, kept only when it is an absolute http or https URL; otherwise ""."""
    if not isinstance(v, str) or not v or len(v) > 2048:
        return ""
    v = v.strip()
    try:
        parts = urllib.parse.urlsplit(v)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or any(c in v for c in "\r\n\t\x00 "):
        return ""
    return v


class ProviderError(Exception):
    """A provider request failed. `status` is the HTTP status, or 0 when the host was unreachable."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RateLimited(ProviderError):
    pass


class ResponseTooLarge(ProviderError):
    def __init__(self, max_bytes: int):
        super().__init__(0, f"the provider sent a response larger than {max_bytes // (1024 * 1024)} MB; it was refused")


def loads(text: str):
    """Parse JSON keeping every non-integer number as an exact Decimal."""
    return json.loads(text, parse_float=Decimal)


def dec(v) -> Optional[Decimal]:
    """A provider number as a Decimal; None when missing, and None (counted) when it is not a usable number."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, bool) or not isinstance(v, (Decimal, int, float, str)):
            raise TypeError(type(v).__name__)
        if isinstance(v, str) and len(v) > 64:
            raise ValueError("too long")
        if isinstance(v, Decimal):
            d = v
        elif isinstance(v, float):
            d = Decimal(repr(v))
        elif isinstance(v, int):
            d = Decimal(v) if abs(v) <= MAX_PROVIDER_NUMBER else Decimal("Infinity")
        else:
            d = Decimal(v.strip())
    except (InvalidOperation, ValueError, TypeError):
        _bad_value(v)
        return None
    if not d.is_finite() or abs(d) > MAX_PROVIDER_NUMBER:
        _bad_value(v)
        return None
    return d


def price(v) -> Optional[Decimal]:
    """A provider price: None when missing or zero (a side with no quote), None and counted when it is
    unreadable, negative or above limits.MAX_PROVIDER_PRICE."""
    d = dec(v)
    if d is None or d == 0:
        return None
    if d < 0 or d > limits.MAX_PROVIDER_PRICE:
        _bad_value(v)
        return None
    return d


_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def parse_ts(s: Optional[str]) -> Optional[float]:
    """RFC-3339 timestamp (up to nanosecond precision, 'Z' or offset) -> UNIX seconds."""
    if not s:
        return None
    s = _FRACTION.sub(r"\1", s.strip()).replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Transport:
    """Performs one GET. Returns (status, headers, body text). Raises OSError when unreachable."""

    def get(self, url: str, headers: dict, timeout: float) -> tuple:
        raise NotImplementedError


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the request carries provider keys in its headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then reports the 3xx answer as an HTTPError


def read_capped(r, max_bytes: int, deadline: float, clock: Callable[[], float] = time.monotonic) -> bytes:
    """Read a response body in chunks, refusing more than max_bytes or a transfer past the deadline."""
    declared = header(dict(r.headers.items()) if getattr(r, "headers", None) else {}, "Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(max_bytes)
    chunks, n = [], 0
    while True:
        chunk = r.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        n += len(chunk)
        if n > max_bytes:
            raise ResponseTooLarge(max_bytes)
        if clock() > deadline:
            raise OSError("the response took too long")
        chunks.append(chunk)


class UrllibTransport(Transport):
    """Standard-library HTTPS client. It honours HTTPS_PROXY like the rest of Python, refuses redirects,
    caps the response size and gives each request an overall deadline."""

    def __init__(self, max_bytes: int = limits.MAX_PROVIDER_RESPONSE):
        self.max_bytes = max_bytes
        self.opener = urllib.request.build_opener(_RefuseRedirects)

    def get(self, url: str, headers: dict, timeout: float) -> tuple:
        req = urllib.request.Request(url, headers=headers, method="GET")
        deadline = time.monotonic() + timeout * 4
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers.items()), read_capped(r, self.max_bytes, deadline).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return e.code, {}, "refused to follow a redirect"
            body = read_capped(e, 64 * 1024, deadline).decode("utf-8", "replace") if e.fp else ""
            return e.code, dict(e.headers.items()) if e.headers else {}, body
        except urllib.error.URLError as e:
            raise OSError(str(e.reason)) from None


class RateLimiter:
    """Token bucket shared by every thread that calls one provider.

    `per_minute` requests are allowed per rolling minute. After a 429 the provider's reset
    time blocks the bucket. Callers that cannot wait long (a web request) pass max_wait and
    get RateLimited instead of hanging.
    """

    def __init__(self, per_minute: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.capacity = float(per_minute)
        self.rate = float(per_minute) / 60.0
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def reserve(self) -> float:
        """Take a token and return how many seconds the caller must wait before using it."""
        with self.lock:
            now = self.clock()
            self._refill(now)
            self.tokens -= 1
            wait = -self.tokens / self.rate if self.tokens < 0 else 0.0
            return max(wait, self.blocked_until - now, 0.0)

    def refund(self) -> None:
        with self.lock:
            self.tokens = min(self.capacity, self.tokens + 1)

    def acquire(self, max_wait: float = 60.0) -> None:
        wait = self.reserve()
        if wait > max_wait:
            self.refund()
            raise RateLimited(429, f"request budget used up; try again in {wait:.0f} s")
        if wait > 0:
            self.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """The provider said 429: block until its reset time and drain the bucket."""
        with self.lock:
            now = self.clock()
            self.blocked_until = max(self.blocked_until, now + max(seconds, 1.0))
            self.tokens = min(self.tokens, 0.0)


def header(headers: dict, name: str) -> Optional[str]:
    name = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def error_message(body: str) -> str:
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            return str(j.get("message") or j.get("error") or j.get("msg") or body)[:300]
    except ValueError:
        pass
    text = (body or "").strip()
    if text.startswith("<"):  # an HTML error page from a proxy or load balancer
        m = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
        text = m.group(1).strip() if m else re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text[:300] or "no details"


class JsonClient:
    """GET + JSON with rate limiting, retry on 5xx and network errors, and backoff on 429."""

    def __init__(self, name: str, headers: dict, transport: Optional[Transport] = None,
                 limiter: Optional[RateLimiter] = None, wall_clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = limits.PROVIDER_TIMEOUT,
                 allow_http: bool = False):
        self.name = name
        self.allow_http = allow_http  # tests only: a provider address must otherwise be https
        self._headers = headers  # holds the credentials; never printed or logged
        self.transport = transport or UrllibTransport()
        self.limiter = limiter or RateLimiter(60)
        self.wall_clock, self.sleep, self.timeout = wall_clock, sleep, timeout
        self.calls = 0
        self.last_error: Optional[str] = None
        self.last_ok: Optional[float] = None

    def get(self, base: str, path: str, params: Optional[dict] = None, max_wait: float = 10.0, retries: int = 2):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None and v != ""})
        url = base.rstrip("/") + path + ("?" + query if query else "")
        host = urllib.parse.urlsplit(base).hostname
        if urllib.parse.urlsplit(base).scheme != "https" and not self.allow_http:
            self.last_error = f"{self.name}: refused the address {base[:80]}: provider addresses must start with https://"
            raise ProviderError(0, self.last_error)
        for attempt in range(retries + 1):
            self.limiter.acquire(max_wait)
            self.calls += 1
            try:
                status, headers, body = self.transport.get(url, self._headers, self.timeout)
            except OSError as e:
                if attempt < retries:
                    self.sleep(0.5 * 2 ** attempt)
                    continue
                self.last_error = f"could not reach {host}: {e}"
                raise ProviderError(0, self.last_error) from None
            if status == 200:
                self.last_ok, self.last_error = self.wall_clock(), None
                try:
                    return loads(body) if body.strip() else None
                except (ValueError, RecursionError):
                    self.last_error = f"{self.name} sent a response that is not valid JSON for {path}"
                    raise ProviderError(status, self.last_error) from None
            msg = error_message(body)
            if status == 429:
                reset = header(headers, "X-RateLimit-Reset")
                try:
                    wait = float(reset) - self.wall_clock() if reset else 0.0
                except ValueError:
                    wait = 0.0
                wait = min(max(wait, 1.0 * 2 ** attempt), 60.0)
                self.limiter.penalize(wait)
                if attempt < retries and wait <= max_wait:
                    self.sleep(wait)
                    continue
                self.last_error = f"{self.name} rate limit reached (HTTP 429); waiting {wait:.0f} s"
                raise RateLimited(429, self.last_error)
            if 300 <= status < 400:
                self.last_error = f"{self.name} answered {path} with a redirect (HTTP {status}); redirects are refused"
                raise ProviderError(status, self.last_error)
            if status >= 500 and attempt < retries:
                self.sleep(0.5 * 2 ** attempt)
                continue
            self.last_error = f"{self.name} returned HTTP {status} for {path}: {msg}"
            raise ProviderError(status, self.last_error)
        raise ProviderError(0, f"{self.name}: request failed")  # not reached
