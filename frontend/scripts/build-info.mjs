// Writes dist/build.json with a hash of the UI sources the bundle was built from. The server compares it
// with the sources next to it (backend/app/api/ui_build.py) and reports `ui_stale` in /api/status when the
// bundle is older than the sources. Keep the two hash definitions identical.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(join(dir, e.name)) : e.isFile() ? [join(dir, e.name)] : []);
}

export function sourceHash() {
  const files = [...walk(join(root, "src")), ...["index.html", "package-lock.json"].map((f) => join(root, f))]
    .filter((f) => existsSync(f))
    .map((f) => relative(root, f).split(sep).join("/"))
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const h = createHash("sha256");
  for (const rel of files) {
    // Line endings are normalised so a Windows checkout (CRLF) hashes like the build machine's.
    const body = readFileSync(join(root, rel)).toString("latin1").replaceAll("\r\n", "\n");
    h.update(rel + "\0" + createHash("sha256").update(Buffer.from(body, "latin1")).digest("hex") + "\n");
  }
  return { algorithm: "sha256-v1", source_hash: h.digest("hex"), files: files.length };
}

writeFileSync(join(root, "dist", "build.json"), JSON.stringify(sourceHash(), null, 2) + "\n");
