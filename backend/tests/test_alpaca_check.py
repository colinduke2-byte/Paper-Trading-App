"""python -m app.marketdata.alpaca --check: one line per check, a summary, no stray output or warnings."""
import re
import warnings

from app.marketdata import alpaca as al
from test_alpaca import FakeTransport, ScriptedConnection, routes

LINE = re.compile(r"^(PASS|FAIL) [a-z ]+: \S.*$")


def run(capfd, **kw):
    lines = []
    code = al._check(seconds=0.5, environ={"APCA_API_KEY_ID": "PK1", "APCA_API_SECRET_KEY": "s", "PAPERDESK_ENV_FILE": ""},
                     out=lines.append, sleep=lambda s: None, **kw)
    cap = capfd.readouterr()
    assert cap.out == "" and cap.err == "", (cap.out, cap.err)  # nothing but the lines below
    assert all(LINE.match(l) for l in lines[:-1]), lines
    assert re.match(r"^SUMMARY: \d of 9 checks passed", lines[-1]), lines
    assert "PK1" not in "\n".join(lines)
    return code, lines


def test_rejected_key_fails_every_check_cleanly(capfd):
    tr = FakeTransport({p: (401, {"message": "unauthorized"}) for p in routes()})

    def refuse(url):
        raise ConnectionRefusedError("refused")
    code, lines = run(capfd, transport=tr, connector=refuse)
    assert code == 1 and len(lines) == 10 and lines[-1].startswith("SUMMARY: 0 of 9")
    assert lines[0].startswith("FAIL assets:")


def test_logged_provider_errors_are_folded_into_the_fail_line(capfd):
    tr = FakeTransport(routes(**{"/v2/stocks/bars": (500, {"message": "boom"})}))
    sent = []
    code, lines = run(capfd, transport=tr, connector=lambda url: ScriptedConnection([], sent))
    bars = next(l for l in lines if l.startswith(("PASS bars", "FAIL bars")))
    assert bars.startswith("FAIL bars: no daily bars returned for SPY (") and code == 1


def test_exit_code_zero_only_when_everything_passes(capfd, monkeypatch):
    monkeypatch.setattr(al, "_run_checks", lambda *a: (a[4]("PASS one: ok"), a[4]("SUMMARY: 5 of 5 checks passed"), 0)[2])
    lines = []
    assert al._check(environ={"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s", "PAPERDESK_ENV_FILE": ""},
                     out=lines.append) == 0


def test_websockets_connection_has_no_deprecation_warning(monkeypatch):
    import websockets.sync.client as wsc

    class FakeCM:
        def __init__(self):
            self.entered = self.exited = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, *a):
            self.exited = True

        def send(self, t):
            pass
    made = []
    monkeypatch.setattr(wsc, "connect", lambda *a, **k: made.append(FakeCM()) or made[-1])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        c = al.WebsocketsConnection("wss://example.invalid/stream")
        c.close()
    assert made[0].entered and made[0].exited
