"""Write THIRD_PARTY_NOTICES.md from the installed runtime dependencies.

Usage (from the repository root, after installing the backend and running `npm ci` in frontend/):
    backend/.venv/bin/python tools/third_party_notices.py

Backend: the runtime dependencies in backend/pyproject.toml (no dev extras) and everything they need,
read from the installed packages' metadata and licence files. Frontend: the "dependencies" of
frontend/package.json (the code bundled into frontend/dist) and everything they need, read from
frontend/node_modules. Build tools (Vite, TypeScript) are not shipped and are not listed.
"""
import importlib.metadata as md
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE_NAMES = re.compile(r"^(licen[cs]e|copying|notice|authors)(\.[a-z]+)?$|^licen[cs]e[-_.]", re.I)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_packages() -> list:
    project = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())["project"]
    todo = [(re.match(r"[A-Za-z0-9_.-]+", r).group(0), set(re.findall(r"\[([^\]]+)\]", r)[0].split(",")) if "[" in r else set())
            for r in project["dependencies"]]
    seen, out = set(), []
    while todo:
        name, extras = todo.pop()
        if norm(name) in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        seen.add(norm(name))
        out.append(dist)
        for req in dist.requires or []:
            m = re.match(r"([A-Za-z0-9_.-]+)(\[[^\]]*\])?[^;]*(;(.*))?$", req)
            marker = m.group(4) or ""
            extra = re.search(r"extra\s*==\s*['\"]([^'\"]+)", marker)
            if extra and extra.group(1) not in extras:
                continue
            if "sys_platform" in marker or "platform_" in marker or "implementation_name" in marker:
                pass  # listed whatever the platform: the notices cover every supported system
            sub = set(m.group(2)[1:-1].split(",")) if m.group(2) else set()
            todo.append((m.group(1), sub))
    return sorted(out, key=lambda d: norm(d.metadata["Name"]))


def python_entry(dist) -> tuple:
    meta = dist.metadata
    lic = meta.get("License-Expression") or ""
    if not lic:
        classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = "; ".join(classifiers) or (meta.get("License") or "").splitlines()[0][:80] or "see licence text"
    url = meta.get("Home-page") or ""
    for u in meta.get_all("Project-URL") or []:
        label, _, link = u.partition(",")
        if not url or label.strip().lower() in ("source", "repository", "homepage"):
            url = link.strip()
    texts = []
    for f in dist.files or []:
        if LICENSE_NAMES.search(Path(str(f)).name) and ".dist-info" in str(f):
            try:
                texts.append((Path(str(f)).name, Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return meta["Name"], dist.version, lic, url, texts


def node_packages() -> list:
    fe = ROOT / "frontend"
    deps = list(json.loads((fe / "package.json").read_text())["dependencies"])
    seen, out = set(), []
    while deps:
        name = deps.pop()
        if name in seen:
            continue
        pdir = fe / "node_modules" / name
        if not (pdir / "package.json").exists():
            continue
        seen.add(name)
        pkg = json.loads((pdir / "package.json").read_text())
        out.append((pdir, pkg))
        deps += list((pkg.get("dependencies") or {}))
    return sorted(out, key=lambda x: x[1]["name"])


def node_entry(pdir: Path, pkg: dict) -> tuple:
    lic = pkg.get("license") or (pkg.get("licenses") or [{}])[0].get("type", "")
    repo = pkg.get("repository") or ""
    url = repo.get("url", "") if isinstance(repo, dict) else repo
    url = re.sub(r"^git\+|\.git$", "", url).replace("git://", "https://")
    texts = [(p.name, p.read_text(encoding="utf-8", errors="replace"))
             for p in sorted(pdir.iterdir()) if p.is_file() and LICENSE_NAMES.search(p.name)]
    return pkg["name"], pkg["version"], lic, pkg.get("homepage") or url, texts


def section(entries: list) -> str:
    rows = ["| Package | Version | Licence | Source |", "|---|---|---|---|"]
    rows += [f"| {n} | {v} | {l} | {u} |" for n, v, l, u, _ in entries]
    body = ["\n".join(rows), ""]
    for n, v, l, u, texts in entries:
        body.append(f"### {n} {v}\n")
        if not texts:
            body.append(f"Licence: {l}. The package ships no licence file; see {u or 'its project page'}.\n")
        for fname, text in texts:
            body.append(f"{fname}:\n\n```text\n{text.strip()}\n```\n")
    return "\n".join(body)


def main() -> int:
    py = [python_entry(d) for d in python_packages()]
    js = [node_entry(*p) for p in node_packages()]
    if not js:
        print("frontend/node_modules is missing: run `npm ci` in frontend/ first.", file=sys.stderr)
        return 1
    text = f"""# Third-party notices

PaperDesk uses the open-source packages below. This file lists each one with its licence and, where the
package ships it, the full licence text. It is generated by `tools/third_party_notices.py` from the
installed packages; regenerate it after changing dependencies.

PaperDesk's own licence has not been chosen yet (there is no LICENSE file); this file covers only the
third-party code.

## Charts: TradingView Lightweight Charts

The price charts are drawn with Lightweight Charts by TradingView, Inc. (Apache License 2.0). As its
NOTICE asks, the app links to https://www.tradingview.com/ from every chart (the logo on the chart and
the "Charting by TradingView" link below the chart and in Help).

## Web interface (bundled into frontend/dist)

{section(js)}
## Server (installed by run.sh, run.bat and the Docker image)

{section(py)}"""
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(text, encoding="utf-8")
    print(f"Wrote THIRD_PARTY_NOTICES.md: {len(js)} frontend and {len(py)} backend packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
