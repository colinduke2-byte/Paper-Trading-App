"""The .env settings file: loaded once, first, for every documented setting; .env.example matches the code."""
import io
import os
import re
from pathlib import Path

from app.api import auth
from app.core import envfile

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"\b((?:PAPERDESK|APCA|FINNHUB)_[A-Z0-9_]+)\b")


def _clean(monkeypatch, env_file):
    for k in list(os.environ):
        if NAME.fullmatch(k) or k in ("HOST", "PORT"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAPERDESK_ENV_FILE", str(env_file))
    monkeypatch.setattr(envfile, "_loaded", False)


def test_default_exchange_honours_data_and_seed_from_env_file(monkeypatch, tmp_path):
    from app.api import server
    from app.marketdata import config
    data = tmp_path / "my data"
    f = tmp_path / ".env"
    f.write_text(f"PAPERDESK_FEED=synthetic\nPAPERDESK_DATA='{data}'\nPAPERDESK_SEED=9\n"
                 "PAPERDESK_SESSIONS=us\n", encoding="utf-8")
    _clean(monkeypatch, f)
    seen = {}
    real = config.select_feed

    def spy(now, data_dir, **kw):
        seen.update(data_dir=data_dir, seed=kw.get("synthetic_seed"))
        return real(now, data_dir, **kw)
    monkeypatch.setattr(config, "select_feed", spy)
    ex = server.default_exchange()
    assert seen == {"data_dir": str(data), "seed": 9}
    assert ex.data_dir == str(data) and ex.session_mode == "us"


def test_environment_wins_over_env_file(monkeypatch, tmp_path):
    from app.api import server
    f = tmp_path / ".env"
    f.write_text(f"PAPERDESK_FEED=synthetic\nPAPERDESK_DATA={tmp_path / 'file'}\n", encoding="utf-8")
    _clean(monkeypatch, f)
    monkeypatch.setenv("PAPERDESK_DATA", str(tmp_path / "env"))
    assert server.default_exchange().data_dir == str(tmp_path / "env")


def test_empty_env_file_setting_means_no_file(monkeypatch, tmp_path):
    assert envfile.env_file_path({"PAPERDESK_ENV_FILE": ""}) is None
    assert envfile.env_file_path({}) == ROOT / ".env"
    assert envfile.load_into(None, {}) == []


def test_load_settings_runs_once(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("PAPERDESK_SEED=5\n", encoding="utf-8")
    _clean(monkeypatch, f)
    assert envfile.load_settings() == ["PAPERDESK_SEED"]
    monkeypatch.delenv("PAPERDESK_SEED")
    assert envfile.load_settings() == [] and "PAPERDESK_SEED" not in os.environ


def test_preflight_reads_host_and_port_from_env_file(tmp_path):
    def run(env):
        out, err = io.StringIO(), io.StringIO()
        return auth.preflight(env, out, err), out.getvalue(), err.getvalue()
    f = tmp_path / ".env"
    f.write_text("PORT=8123  # mine\nHOST=localhost\n", encoding="utf-8")
    assert run({"PAPERDESK_ENV_FILE": str(f)})[:2] == (0, "localhost 8123\n")
    assert run({"PAPERDESK_ENV_FILE": str(f), "PORT": "9000"})[:2] == (0, "localhost 9000\n")
    # unsafe values from the file are refused before they reach a shell or batch parser
    f.write_text('PORT=80 & calc\n', encoding="utf-8")
    code, out, err = run({"PAPERDESK_ENV_FILE": str(f)})
    assert code == 1 and out == "" and "PORT must be a number" in err
    f.write_text('HOST=0.0.0.0\n', encoding="utf-8")
    code, out, err = run({"PAPERDESK_ENV_FILE": str(f)})
    assert code == 1 and out == "" and "without a passcode" in err


def test_run_scripts_take_host_and_port_from_preflight():
    sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    bat = (ROOT / "run.bat").read_text(encoding="utf-8")
    assert 'read -r HOST PORT <<<"$BIND"' in sh and '--port "$PORT"' in sh
    assert 'tokens=1,2' in bat and 'set "PORT=%BINDPORT%"' in bat
    # no default PORT before the preflight: it would hide PORT in .env
    assert 'set "PORT=8000"' not in bat
    # the batch parser never sees the settings file's contents
    assert "type " not in bat.lower() and ".env\" " not in bat


def _documented():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        m = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.add(m.group(1))
    return names


def _read_by_code():
    names = set()
    files = list((ROOT / "backend" / "app").rglob("*.py")) + [ROOT / f for f in
                                                                ("run.sh", "run.bat", "docker-compose.yml", "Dockerfile")]
    for p in files:
        names |= set(NAME.findall(p.read_text(encoding="utf-8")))
    return names


def test_env_example_lists_exactly_the_settings_the_code_reads():
    documented, read = _documented(), _read_by_code() | {"HOST", "PORT"}
    assert read - documented == set(), "read by the code but missing from .env.example"
    assert documented - read == set(), "listed in .env.example but never read"
