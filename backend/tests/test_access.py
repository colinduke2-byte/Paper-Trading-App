"""Passcode sign-in, LAN mode settings and request hardening (app/api/auth.py, routes_auth.py)."""
import io
import os
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth
from app.api.server import create_app
from app.exchange import Exchange
from app.marketdata.synthetic import SyntheticFeed

PASS = "correct horse 42"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def ui_dir(tmp_path, monkeypatch):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text('<!doctype html><div id="root"></div>')
    (d / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setenv("PAPERDESK_UI", str(d))
    return d


def make(monkeypatch, data_dir=None, run_loop=False, client=("testclient", 50000), **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ex = Exchange(SyntheticFeed(seed=3, now=time.time()), data_dir=data_dir)
    if not ex.load():
        ex.create_account("Margin", "margin")
    ex.tick()
    app = create_app(ex, run_loop=run_loop, tick_seconds=0.02)
    clock = Clock()
    app.state.access.clock = clock
    return TestClient(app, client=client), app, clock


def locked(monkeypatch, **env):
    return make(monkeypatch, PAPERDESK_PASSCODE=PASS, **env)


def login(c, passcode=PASS, **kw):
    return c.post("/api/auth/login", json={"passcode": passcode}, **kw)


# ---- mode off -------------------------------------------------------------------------------

def test_mode_off_needs_no_sign_in(monkeypatch):
    c, _, _ = make(monkeypatch)
    assert c.get("/api/accounts").status_code == 200
    assert c.get("/api/auth/state").json() == {"auth_required": False, "lan": False, "bypass": False,
                                               "authenticated": True}
    assert login(c).status_code == 400
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": []})


# ---- configuration -------------------------------------------------------------------------------

def test_lan_mode_without_passcode_refuses_to_start(monkeypatch):
    with pytest.raises(auth.ConfigError, match="needs a passcode"):
        make(monkeypatch, PAPERDESK_LAN="1")
    with pytest.raises(auth.ConfigError, match="too short"):
        make(monkeypatch, PAPERDESK_LAN="1", PAPERDESK_PASSCODE="short")


def test_config_validation():
    assert auth.load_config({}) == auth.Config()
    assert not auth.load_config({"PAPERDESK_LAN": "0", "PAPERDESK_PASSCODE": ""}).auth
    cfg = auth.load_config({"PAPERDESK_LAN": "yes", "PAPERDESK_PASSCODE": PASS, "PAPERDESK_SESSION_DAYS": "2",
                            "PAPERDESK_LOOPBACK_BYPASS": "0", "PAPERDESK_ALLOWED_HOSTS": "MyPC.local, nas.lan"})
    assert cfg.lan and cfg.auth and cfg.session_days == 2 and not cfg.loopback_bypass
    assert cfg.allowed_hosts == ("mypc.local", "nas.lan")
    assert PASS not in repr(cfg)
    for bad in ({"PAPERDESK_LAN": "maybe"}, {"PAPERDESK_SESSION_DAYS": "0"}, {"PAPERDESK_SESSION_DAYS": "x"},
                {"PAPERDESK_ALLOWED_HOSTS": "*.example.com"}, {"PAPERDESK_BIND": "0.0.0.0"}):
        with pytest.raises(auth.ConfigError):
            auth.load_config(bad)
    # Docker: publishing on the network is allowed only together with LAN mode and a passcode.
    assert auth.load_config({"PAPERDESK_BIND": "0.0.0.0", "PAPERDESK_LAN": "1", "PAPERDESK_PASSCODE": PASS}).lan
    assert not auth.load_config({"PAPERDESK_BIND": "127.0.0.1"}).lan


def test_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("﻿# comment\nPAPERDESK_LAN=1   # on\nexport PAPERDESK_PASSCODE='p#ss word $x'\n"
                 "PAPERDESK_SEED=9\nPAPERDESK_SESSION_DAYS=\"5\"\n", encoding="utf-8")
    cfg = auth.load_config({"PAPERDESK_ENV_FILE": str(f)})
    assert cfg.lan and cfg.passcode == "p#ss word $x" and cfg.session_days == 5
    assert auth.load_config({"PAPERDESK_ENV_FILE": str(f), "PAPERDESK_SESSION_DAYS": "7"}).session_days == 7
    assert not auth.load_config({"PAPERDESK_ENV_FILE": str(tmp_path / "missing")}).lan


def test_preflight(monkeypatch):
    def run(env):
        out, err = io.StringIO(), io.StringIO()
        return auth.preflight(env, out, err), out.getvalue(), err.getvalue()

    assert run({}) == (0, "127.0.0.1\n", "")
    code, out, err = run({"HOST": "0.0.0.0"})
    assert code == 1 and out == "" and "without a passcode" in err
    code, out, err = run({"PAPERDESK_LAN": "1"})
    assert code == 1 and out == "" and "needs a passcode" in err
    monkeypatch.setattr(auth, "lan_addresses", lambda: ["192.168.1.20", "10.0.0.5"])
    code, out, err = run({"PAPERDESK_LAN": "1", "PAPERDESK_PASSCODE": PASS, "PORT": "8123"})
    assert code == 0 and out == "0.0.0.0\n"
    assert "http://192.168.1.20:8123" in err and "http://10.0.0.5:8123" in err
    assert "same Wi-Fi" in err and "Private networks only" in err and "plain HTTP" in err
    assert PASS not in err + out
    monkeypatch.setattr(auth, "lan_addresses", lambda: [])
    code, _, err = run({"PAPERDESK_LAN": "1", "PAPERDESK_PASSCODE": PASS})
    assert code == 0 and "ipconfig" in err


def test_lan_addresses_are_private_ipv4(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 2, 17, "", (ip, 0)) for ip in
                                                                ("127.0.1.1", "192.168.1.20", "8.8.8.8", "169.254.1.1")])

    class NoRoute:
        def __init__(self, *a): pass
        def connect(self, addr): raise OSError("no route")
        def close(self): pass
    monkeypatch.setattr(socket, "socket", NoRoute)
    assert auth.lan_addresses() == ["192.168.1.20"]


# ---- authentication -----------------------------------------------------------------------------

def test_unauthenticated_api_and_ws_are_refused_but_ui_shell_loads(monkeypatch, ui_dir):
    c, _, _ = locked(monkeypatch)
    page = c.get("/")
    assert page.status_code == 200 and '<div id="root">' in page.text
    assert c.get("/some/deep/link").status_code == 200
    assert c.get("/assets/app.js").status_code == 200
    for path in ("/api/status", "/api/accounts", "/api/quotes?symbols=NVLX", "/docs", "/openapi.json"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert "Margin" not in r.text and "NVLX" not in r.text
    assert c.get("/api/auth/state").json()["authenticated"] is False
    assert c.post("/api/accounts", json={"name": "x"}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_login_failure_and_success(monkeypatch):
    c, _, _ = locked(monkeypatch)
    r = login(c, "wrong passcode")
    assert r.status_code == 200 and r.json()["ok"] is False and "not correct" in r.json()["detail"]
    assert auth.COOKIE not in c.cookies and "set-cookie" not in r.headers
    assert c.post("/api/auth/login", json={"passcode": 123}).status_code == 400
    assert c.post("/api/auth/login", content=b"x" * 5000).status_code == 413
    r = login(c)
    assert r.json() == {"ok": True}
    token = c.cookies[auth.COOKIE]
    assert len(token) >= 40 and PASS not in token
    assert c.get("/api/accounts").status_code == 200
    assert c.get("/api/auth/state").json() == {"auth_required": True, "lan": False, "bypass": False,
                                               "authenticated": True}
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": []})


def test_cookie_flags(monkeypatch):
    c, _, _ = locked(monkeypatch, PAPERDESK_SESSION_DAYS="30")
    cookie = login(c).headers["set-cookie"]
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0].startswith(auth.COOKIE + "=")
    assert "HttpOnly" in parts and "SameSite=Strict" in parts and "Path=/" in parts
    assert f"Max-Age={30 * 86400}" in parts and "Secure" not in parts
    https = TestClient(c.app, base_url="https://testserver")
    assert "Secure" in [p.strip() for p in login(https).headers["set-cookie"].split(";")]


def test_constant_time_compare_is_used(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)
    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    c, _, _ = locked(monkeypatch)
    calls.clear()
    login(c, "x")
    login(c)
    assert calls == [(32, 32), (32, 32)]  # digests: the passcode length is not revealed either


def test_session_expires_when_idle(monkeypatch):
    c, _, clock = locked(monkeypatch, PAPERDESK_SESSION_DAYS="30")
    login(c)
    clock.advance(20 * 86400)
    assert c.get("/api/accounts").status_code == 200      # use extends the idle timer
    clock.advance(20 * 86400)
    assert c.get("/api/accounts").status_code == 200
    clock.advance(30 * 86400 + 1)
    assert c.get("/api/accounts").status_code == 401


def test_cookie_is_refreshed_after_a_day(monkeypatch):
    c, _, clock = locked(monkeypatch)
    login(c)
    assert "set-cookie" not in c.get("/api/accounts").headers
    clock.advance(86400 + 1)
    cookie = c.get("/api/accounts").headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "set-cookie" not in c.get("/api/accounts").headers


def test_logout(monkeypatch):
    c, _, _ = locked(monkeypatch)
    login(c)
    token = c.cookies[auth.COOKIE]
    r = c.post("/api/auth/logout")
    assert r.json() == {"ok": True} and "Max-Age=0" in r.headers["set-cookie"]
    assert c.get("/api/accounts").status_code == 401
    other = TestClient(c.app, cookies={auth.COOKIE: token})  # the old token is dead server-side, too
    assert other.get("/api/accounts").status_code == 401


def test_open_stream_is_closed_on_logout(monkeypatch):
    c, app, _ = make(monkeypatch, run_loop=True, PAPERDESK_PASSCODE=PASS)
    with c:
        login(c)
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"symbols": ["NVLX"]})
            assert ws.receive_json()["type"] == "tick"
            c.post("/api/auth/logout")
            with pytest.raises(WebSocketDisconnect) as e:
                for _ in range(100):
                    ws.receive_json()
            assert e.value.code == 4401


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_bypass(monkeypatch, addr):
    c, _, _ = locked(monkeypatch, client=(addr, 5000))
    assert c.get("/api/accounts").status_code == 200
    assert c.get("/api/auth/state").json()["bypass"] is True
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": []})


def test_loopback_bypass_off(monkeypatch):
    c, _, _ = locked(monkeypatch, client=("127.0.0.1", 5000), PAPERDESK_LOOPBACK_BYPASS="0")
    assert c.get("/api/accounts").status_code == 401
    assert c.get("/api/auth/state").json()["bypass"] is False
    login(c)
    assert c.get("/api/accounts").status_code == 200


def test_lan_clients_never_bypass(monkeypatch):
    c, _, _ = locked(monkeypatch, client=("192.168.1.30", 5000), PAPERDESK_LAN="1")
    assert c.get("/api/accounts").status_code == 401


# ---- brute force protection -----------------------------------------------------------------------

def test_backoff_per_client(monkeypatch, caplog):
    c, app, clock = locked(monkeypatch, client=("192.168.1.30", 5000), PAPERDESK_LAN="1")
    caplog.set_level("INFO", logger="paperdesk.access")
    for i in range(1, 3):
        r = login(c, f"guess-{i}xx")
        assert r.json()["ok"] is False and r.json()["retry_after"] == 0
    assert login(c, "guess-3xx").json()["retry_after"] == 1     # third failure: wait 1 s
    r = login(c)                                                # even the right passcode is not checked now
    assert r.status_code == 429 and r.headers["retry-after"] == "1" and "Try again" in r.json()["detail"]
    clock.advance(1)
    assert login(c, "guess-4xx").json()["retry_after"] == 2
    clock.advance(1.5)
    assert login(c).status_code == 429
    clock.advance(0.5)
    assert login(c).json() == {"ok": True}                      # success clears the record
    assert login(c, "guess-5xx").json()["retry_after"] == 0
    # Another client is not affected by this one's failures.
    other = TestClient(app, client=("192.168.1.31", 5000))
    assert login(other).json() == {"ok": True}
    text = caplog.text
    assert "failed sign-in from 192.168.1.30" in text
    assert "guess-" not in text and PASS not in text and c.cookies[auth.COOKIE] not in text


def test_backoff_grows_exponentially_to_a_cap():
    clock = Clock()
    lim = auth.Limiter(clock)
    delays = [lim.failed("a")[1] for _ in range(16)]
    assert delays[:6] == [0, 0, 1, 2, 4, 8] and delays[-1] == auth.Limiter.MAX_DELAY
    assert lim.retry_after("a") == auth.Limiter.MAX_DELAY
    clock.advance(auth.Limiter.FORGET)
    assert lim.retry_after("a") == 0 and "a" not in lim.clients


def test_ipv6_clients_share_a_bucket_per_64():
    assert auth.client_key("2001:db8:1:2::1") == auth.client_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert auth.client_key("::ffff:192.168.1.5") == "192.168.1.5"


def test_global_cap(monkeypatch):
    c, app, clock = locked(monkeypatch, PAPERDESK_LAN="1")
    for i in range(auth.Limiter.GLOBAL_MAX):
        r = login(TestClient(app, client=(f"10.0.{i}.1", 5000)), "wrong-passcode")
        assert r.json()["ok"] is False
    fresh = TestClient(app, client=("10.9.9.9", 5000))
    assert login(fresh).status_code == 429
    clock.advance(auth.Limiter.WINDOW)
    assert login(fresh).json() == {"ok": True}


# ---- cross-site protections ------------------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"PAPERDESK_PASSCODE": PASS}])
def test_cross_origin_writes_are_refused(monkeypatch, env):
    c, _, _ = make(monkeypatch, client=("127.0.0.1", 5000), **env)
    body = {"name": "x", "type": "cash", "starting_cash": 10}
    for headers in ({"origin": "http://evil.example"}, {"origin": "null"}, {"origin": "http://testserver:8000"},
                    {"origin": "https://testserver"}, {"sec-fetch-site": "cross-site"},
                    {"sec-fetch-site": "same-site", "origin": "http://testserver"}):
        r = c.post("/api/accounts", json=body, headers=headers)
        assert r.status_code == 403, headers
        assert "another website" in r.json()["detail"]
    assert c.post("/api/accounts", json=body, headers={"origin": "http://testserver",
                                                       "sec-fetch-site": "same-origin"}).status_code == 200
    assert c.post("/api/accounts", json=body).status_code == 200          # non-browser client
    assert c.get("/api/accounts", headers={"origin": "http://evil.example"}).status_code == 200  # reads are safe
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"symbols": []})


def test_origin_matching_with_ports():
    h = lambda **kw: kw  # noqa: E731
    ok = auth.Access.origin_ok
    assert ok(h(origin="http://192.168.1.20:8000"), "192.168.1.20:8000")
    assert ok(h(origin="http://localhost:5173"), "localhost:5173")
    assert ok(h(origin="http://[::1]:8000"), "[::1]:8000")
    assert not ok(h(origin="http://192.168.1.20:8001"), "192.168.1.20:8000")
    assert not ok(h(origin="http://192.168.1.20.evil.example:8000"), "192.168.1.20:8000")
    assert not ok(h(origin="http://localhost:8000"), "localhost")
    assert not ok(h(origin="ftp://localhost"), "localhost")
    assert not ok(h(origin="https://localhost"), "localhost")
    assert ok(h(origin="https://localhost"), "localhost", "https")


def test_host_check_blocks_dns_rebinding(monkeypatch):
    c, _, _ = make(monkeypatch)
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST"):
        assert c.get("/api/status", headers={"host": host}).status_code == 200, host
    for host in ("evil.example:8000", "attacker.example", "192.168.1.20:8000", "localhost.evil.example",
                 "", "bad host", "[::1", "localhost:abc", "[fe80::1%eth0;x]:80"):
        r = c.get("/api/status", headers={"host": host})
        assert r.status_code == 421, host
        assert "host name" in r.json()["detail"]
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"host": "evil.example:8000"}) as ws:
            ws.receive_json()


def test_host_check_in_lan_mode(monkeypatch):
    c, _, _ = locked(monkeypatch, client=("127.0.0.1", 5000), PAPERDESK_LAN="1", PAPERDESK_ALLOWED_HOSTS="mypc.local")
    for host in ("192.168.1.20:8000", "10.0.0.5", "mypc.local:8000", "localhost"):
        assert c.get("/api/status", headers={"host": host}).status_code == 200, host
    for host in ("evil.example:8000", "otherpc.local"):
        assert c.get("/api/status", headers={"host": host}).status_code == 421, host


def test_security_headers(monkeypatch, ui_dir):
    c, _, _ = locked(monkeypatch)
    for path in ("/", "/api/auth/state", "/api/accounts", "/assets/app.js"):
        r = c.get(path, headers={"host": "localhost:8000"})
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-frame-options"] == "DENY"
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "default-src" in csp
    csp = c.get("/", headers={"host": "localhost:8000"}).headers["content-security-policy"]
    assert "script-src 'self'" in csp and "ws://localhost:8000" in csp and "unsafe-eval" not in csp
    assert c.get("/api/auth/state").headers["cache-control"] == "no-store"
    assert c.get("/api/accounts").headers["cache-control"] == "no-store"


# ---- persistence -------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_sessions_survive_restart_and_are_stored_safely(monkeypatch, tmp_path):
    c, _, _ = locked(monkeypatch, data_dir=str(tmp_path))
    login(c)
    token = c.cookies[auth.COOKIE]
    path = tmp_path / "sessions.json"
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert token not in text and PASS not in text
    c2, _, _ = locked(monkeypatch, data_dir=str(tmp_path))
    c2.cookies.set(auth.COOKIE, token)
    assert c2.get("/api/accounts").status_code == 200
    # A changed passcode signs everyone out.
    c3, _, _ = make(monkeypatch, data_dir=str(tmp_path), PAPERDESK_PASSCODE="another passcode")
    c3.cookies.set(auth.COOKIE, token)
    assert c3.get("/api/accounts").status_code == 401


def test_unreadable_session_file_starts_empty(monkeypatch, tmp_path):
    (tmp_path / "sessions.json").write_text("{not json")
    c, _, _ = locked(monkeypatch, data_dir=str(tmp_path))
    assert c.get("/api/accounts").status_code == 401
    assert login(c).json() == {"ok": True}


# ---- review fixes --------------------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("weak", ["password", "PASSCODE", "12345678", "aaaaaaaa", "abababab", "        ",
                                  "  ab12  ", "changeme", "Choose a long passcode", "qwertyuiop", "Pass Word"])
def test_weak_and_placeholder_passcodes_are_refused(weak):
    with pytest.raises(auth.ConfigError, match="example text or too simple"):
        auth.load_config({"PAPERDESK_PASSCODE": weak})


def test_example_placeholders_are_refused(tmp_path):
    # The .env.example line, simply uncommented, must not start a server.
    line = next(l for l in _read(".env.example").splitlines() if l.startswith("# PAPERDESK_PASSCODE="))
    env = tmp_path / ".env"
    env.write_text("PAPERDESK_LAN=1\n" + line[2:] + "\n")
    with pytest.raises(auth.ConfigError, match="example text"):
        auth.load_config({"PAPERDESK_ENV_FILE": str(env)})
    compose = next(l for l in _read("docker-compose.yml").splitlines() if "PAPERDESK_PASSCODE=" in l)
    with pytest.raises(auth.ConfigError, match="example text"):
        auth.load_config({"PAPERDESK_PASSCODE": compose.split("=", 1)[1].strip().strip("'")})
    readme = next(l for l in _read("README.md").splitlines() if l.startswith("PAPERDESK_PASSCODE="))
    with pytest.raises(auth.ConfigError, match="example text"):
        auth.load_config({"PAPERDESK_PASSCODE": readme.split("=", 1)[1].strip().strip("'")})
    assert auth.load_config({"PAPERDESK_PASSCODE": PASS}).auth


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "Via"])
def test_proxied_loopback_requests_do_not_bypass(monkeypatch, header):
    c, _, _ = locked(monkeypatch, client=("127.0.0.1", 5000))
    assert c.get("/api/accounts").status_code == 200
    assert c.get("/api/accounts", headers={header: "127.0.0.1"}).status_code == 401
    assert c.get("/api/auth/state", headers={header: "127.0.0.1"}).json()["bypass"] is False


def test_launchers_ignore_proxy_headers_and_docker_disables_the_bypass():
    import yaml
    assert "--no-proxy-headers" in _read("Dockerfile")
    assert "--no-proxy-headers" in _read("run.sh")
    assert "--no-proxy-headers" in _read("run.bat")
    assert '--port "%PORT%"' in _read("run.bat")
    env = yaml.safe_load(_read("docker-compose.yml"))["services"]["paperdesk"]["environment"]
    assert env["PAPERDESK_LOOPBACK_BYPASS"] == "0"


def test_every_route_is_under_api_or_ws_or_known(monkeypatch, ui_dir):
    """A new routes_*.py that adds a route outside /api must be a deliberate choice (and is protected
    anyway, see the next test)."""
    from starlette.routing import Mount
    _, app, _ = locked(monkeypatch)
    allowed = {"/assets", "/{path:path}", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/print.js"}
    stray = [r.path for r in app.routes
             if r.path not in allowed and not r.path.startswith("/api/") and r.path != "/ws"]
    assert stray == []
    assert any(isinstance(r, Mount) and r.path == "/assets" for r in app.routes)


def test_routes_outside_api_are_protected_by_default(monkeypatch, ui_dir):
    c, app, _ = locked(monkeypatch)
    app.add_api_route("/metrics", lambda: {"secret": 1})
    app.add_api_websocket_route("/feed", lambda ws: None)
    assert c.get("/metrics").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/feed") as ws:
            ws.receive_json()
    for public in ("/", "/trade/MKTX", "/assets/app.js", "/print.js", "/api/auth/state"):
        assert c.get(public).status_code == 200, public


def test_print_script_for_server_rendered_pages(monkeypatch):
    c, _, _ = locked(monkeypatch)
    r = c.get("/print.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "data-print" in r.text and "window.print()" in r.text
    csp = c.get("/api/auth/state").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_preflight_validates_host_and_port():
    def run(env):
        out, err = io.StringIO(), io.StringIO()
        return auth.preflight(env, out, err), out.getvalue(), err.getvalue()
    for env in ({"HOST": "127.0.0.1 & calc"}, {"HOST": "a\"b"}, {"PORT": "80 & x"}, {"PORT": "0"}, {"PORT": "70000"}):
        code, out, err = run(env)
        assert code == 1 and out == "", env
    assert run({"HOST": "localhost", "PORT": "8123"})[:2] == (0, "localhost\n")
    assert run({"HOST": "::1"})[:2] == (0, "::1\n")


def test_sign_ins_survive_replay_and_stay_in_the_live_data_dir(monkeypatch, tmp_path):
    from app.marketdata.replay.samples import SAMPLES
    live = tmp_path / "live"
    c, app, _ = locked(monkeypatch, data_dir=str(live))
    assert login(c).json() == {"ok": True}
    token = c.cookies[auth.COOKIE]
    assert app.state.access.sessions.path == str(live / "sessions.json")
    assert c.post("/api/replay/start", json={"session": SAMPLES[0][0]}).status_code == 200
    assert c.get("/api/status").json()["mode"] == "replay"
    assert c.get("/api/replay/status").json()["active"] is True
    assert c.get("/api/accounts").status_code == 200
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"symbols": []})
    anon = TestClient(app)
    assert anon.get("/api/replay/status").status_code == 401
    assert anon.post("/api/replay/exit").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/ws") as ws:
            ws.receive_json()
    second = TestClient(app)
    assert login(second).json() == {"ok": True}        # a sign-in during replay is a live sign-in too
    assert c.post("/api/replay/exit").json() == {"active": False}
    assert c.get("/api/accounts").status_code == 200 and second.get("/api/accounts").status_code == 200
    assert not [p for p in tmp_path.rglob("sessions.json") if p != live / "sessions.json"]
    assert token not in (live / "sessions.json").read_text()
