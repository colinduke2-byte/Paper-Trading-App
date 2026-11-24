import { defineConfig, devices } from "@playwright/test";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// The port and data directory are chosen once and shared with the worker processes through the
// environment, so the server and the tests agree on them. Override the port with E2E_PORT.
process.env.E2E_PORT ||= String(20000 + Math.floor(Math.random() * 20000));
process.env.E2E_DATA ||= mkdtempSync(join(tmpdir(), "paperdesk-e2e-"));
// A second server with a passcode and the loopback bypass off, for the sign-in tests (auth.spec.ts).
process.env.E2E_AUTH_PORT ||= String(Number(process.env.E2E_PORT) + 1);
process.env.E2E_AUTH_DATA ||= mkdtempSync(join(tmpdir(), "paperdesk-e2e-auth-"));
process.env.E2E_PASSCODE ||= "e2e passcode 2468";

const port = process.env.E2E_PORT;
const authPort = process.env.E2E_AUTH_PORT;
const root = resolve(__dirname, "..");
const venvPython = join(root, "backend", ".venv", "bin", "python");
const python = process.env.E2E_PYTHON || (existsSync(venvPython) ? venvPython : "python3");
// A pre-installed browser can be used instead of the one Playwright downloads.
const executablePath = process.env.E2E_CHROMIUM || undefined;

// Shared by both servers. The access settings are cleared so a developer's shell cannot change the tests.
const common = {
  PAPERDESK_UI: join(root, "frontend", "dist"),
  PAPERDESK_SESSIONS: "always",
  PAPERDESK_SEED: "7",
  PAPERDESK_LAN: "",
  PAPERDESK_PASSCODE: "",
  PAPERDESK_LOOPBACK_BYPASS: "",
  PAPERDESK_ENV_FILE: "",
};

function phone(name: string) {
  const { defaultBrowserType: _ignored, ...rest } = devices[name];
  return { ...rest, browserName: "chromium" as const };
}

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
    launchOptions: executablePath ? { executablePath } : {},
  },
  projects: [
    { name: "desktop", testIgnore: /(mobile|auth)\.spec/ },
    // Phone profiles run in Chromium (the only browser pre-installed here). Device emulation sets viewport, touch and scale factor.
    { name: "iphone-13", testMatch: /mobile\.spec/, use: { ...phone("iPhone 13") } },
    { name: "pixel-7", testMatch: /mobile\.spec/, use: { ...phone("Pixel 7") } },
    // Sign-in tests run against the passcode server.
    { name: "auth-desktop", testMatch: /auth\.spec/, use: { baseURL: `http://127.0.0.1:${authPort}` } },
    { name: "auth-pixel-7", testMatch: /auth\.spec/, use: { ...phone("Pixel 7"), baseURL: `http://127.0.0.1:${authPort}` } },
  ],
  webServer: [
    {
      command: `"${python}" -m uvicorn app.api.server:app_factory --factory --host 127.0.0.1 --port ${port} --no-proxy-headers`,
      cwd: join(root, "backend"),
      url: `http://127.0.0.1:${port}/api/status`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { ...common, PAPERDESK_DATA: process.env.E2E_DATA! },
    },
    {
      command: `"${python}" -m uvicorn app.api.server:app_factory --factory --host 127.0.0.1 --port ${authPort} --no-proxy-headers`,
      cwd: join(root, "backend"),
      url: `http://127.0.0.1:${authPort}/api/auth/state`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { ...common, PAPERDESK_DATA: process.env.E2E_AUTH_DATA!, PAPERDESK_PASSCODE: process.env.E2E_PASSCODE!,
             PAPERDESK_LOOPBACK_BYPASS: "0" },
    },
  ],
});
