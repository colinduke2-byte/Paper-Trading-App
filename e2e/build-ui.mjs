// Builds frontend/dist so the test server has a UI to serve. Skipped when E2E_SKIP_BUILD=1.
import { execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

if (process.env.E2E_SKIP_BUILD !== "1") {
  const frontend = join(dirname(fileURLToPath(import.meta.url)), "..", "frontend");
  const run = (cmd) => execSync(cmd, { cwd: frontend, stdio: "inherit" });
  if (!existsSync(join(frontend, "node_modules"))) run("npm ci");
  run("npm run build");
}
