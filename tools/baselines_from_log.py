"""Extract visual baselines that CI printed into a job log (step "Print new or changed visual baselines").

GitHub job logs are text, and artifact downloads are not always reachable, so the e2e job prints new or
changed screenshots as a base64 tar.gz between two marker lines. Usage, from the repository root:

    gh api repos/<owner>/<repo>/actions/jobs/<job id>/logs > job.log
    python tools/baselines_from_log.py job.log [more logs...]

The files land under e2e/tests/__screenshots__/; review them (they are PNGs) and commit them.
"""
import base64
import io
import re
import sys
import tarfile

BEGIN, END = "----- BEGIN PAPERDESK BASELINES -----", "----- END PAPERDESK BASELINES -----"
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")


def extract(path: str) -> list[str]:
    lines, inside, out = [], False, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = STAMP.sub("", raw.rstrip("\r\n"))
            if line.strip() == BEGIN:
                inside, lines = True, []
            elif line.strip() == END and inside:
                inside = False
                data = base64.b64decode("".join(lines))
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                    for m in tar.getmembers():
                        if not m.isfile() or not m.name.startswith("e2e/tests/__screenshots__/") or ".." in m.name \
                                or not m.name.endswith(".png"):
                            raise SystemExit(f"Refusing unexpected entry in the log: {m.name}")
                    tar.extractall(".", filter="data")
                    out += [m.name for m in tar.getmembers()]
            elif inside:
                lines.append(line.strip())
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        for name in extract(p):
            print(name)
