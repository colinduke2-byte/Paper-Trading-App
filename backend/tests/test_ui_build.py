"""The stale-UI guard: dist/build.json against the UI sources next to it."""
import json
import os
import shutil
import subprocess

import pytest

from app.api import ui_build

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")


def _tree(tmp_path, crlf=False):
    fe = tmp_path / "frontend"
    (fe / "src" / "sub").mkdir(parents=True)
    nl = "\r\n" if crlf else "\n"
    (fe / "src" / "App.tsx").write_bytes(f"export const a = 1;{nl}".encode())
    (fe / "src" / "sub" / "x.css").write_bytes(f"body {{}}{nl}".encode())
    (fe / "index.html").write_bytes(f"<div id=\"root\"></div>{nl}".encode())
    (fe / "package-lock.json").write_bytes(b"{}")
    (fe / "dist").mkdir()
    return fe


def _stamp(fe, value=None):
    (fe / "dist" / "build.json").write_text(json.dumps(
        {"algorithm": "sha256-v1", "source_hash": value or ui_build.source_hash(str(fe))}))


def test_fresh_build_is_not_stale_and_an_edit_makes_it_stale(tmp_path):
    fe = _tree(tmp_path)
    _stamp(fe)
    assert ui_build.check(str(fe / "dist")) is False
    (fe / "src" / "App.tsx").write_text("export const a = 2;\n")
    assert ui_build.check(str(fe / "dist")) is True


def test_line_endings_do_not_matter(tmp_path):
    lf = ui_build.source_hash(str(_tree(tmp_path / "a")))
    crlf = ui_build.source_hash(str(_tree(tmp_path / "b", crlf=True)))
    assert lf == crlf


def test_silent_without_sources_or_build_info(tmp_path):
    fe = _tree(tmp_path)
    assert ui_build.check(str(fe / "dist")) is False  # no build.json
    _stamp(fe, "0" * 64)
    shutil.rmtree(fe / "src")
    assert ui_build.check(str(fe / "dist")) is False  # a ZIP or Docker install: no sources


def test_status_reports_ui_stale(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    from helpers import make_exchange
    fe = _tree(tmp_path)
    (fe / "dist" / "assets").mkdir()
    (fe / "dist" / "index.html").write_text("<div id=\"root\"></div>")
    _stamp(fe, "0" * 64)
    monkeypatch.setenv("PAPERDESK_UI", str(fe / "dist"))
    ex, _, _ = make_exchange()
    assert TestClient(create_app(ex, run_loop=False)).get("/api/status").json()["ui_stale"] is True
    _stamp(fe)
    assert TestClient(create_app(ex, run_loop=False)).get("/api/status").json()["ui_stale"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_build_script_computes_the_same_hash(tmp_path):
    fe = _tree(tmp_path, crlf=True)
    (fe / "scripts").mkdir()
    shutil.copy(os.path.join(FRONTEND, "scripts", "build-info.mjs"), fe / "scripts" / "build-info.mjs")
    subprocess.run(["node", str(fe / "scripts" / "build-info.mjs")], check=True)
    info = json.loads((fe / "dist" / "build.json").read_text())
    assert info["source_hash"] == ui_build.source_hash(str(fe))
