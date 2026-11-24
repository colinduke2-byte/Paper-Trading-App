import { defineConfig, devices, type Project } from "@playwright/test";
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

// E2E_BASE_URL: test a server that is already running (CI starts it with run.bat on Windows) instead of
// starting one here. The sign-in server is still started here.
const external = process.env.E2E_BASE_URL;
const port = process.env.E2E_PORT;
const authPort = process.env.E2E_AUTH_PORT;
const root = resolve(__dirname, "..");
const venvPython = [join(root, "backend", ".venv", "bin", "python"), join(root, "backend", ".venv", "Scripts", "python.exe")]
  .find((p) => existsSync(p));
const python = process.env.E2E_PYTHON || venvPython || "python3";
// A pre-installed browser can be used instead of the one Playwright downloads.
const executablePath = process.env.E2E_CHROMIUM || undefined;
// Which browser engines to run: chromium (the default, fast locally), firefox, webkit, or a comma-separated
// list. CI runs each engine in its own job. E2E_CHANNEL=msedge runs the Chromium projects in Microsoft Edge.
const engines = new Set((process.env.E2E_ENGINES || "chromium").split(",").map((s) => s.trim()).filter(Boolean));
const channel = process.env.E2E_CHANNEL || undefined;

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

const DESKTOP = { width: 1440, height: 900 };
const authURL = `http://127.0.0.1:${authPort}`;

// A device profile on a chosen engine. Chromium emulates the size, touch and scale of an iPhone but not
// Safari itself; the WebKit profiles run the real Safari engine.
function device(name: string, browserName: "chromium" | "webkit" | "firefox" = devices[name].defaultBrowserType) {
  const { defaultBrowserType: _ignored, ...rest } = devices[name];
  return { ...rest, browserName };
}
function chromiumOpts() {
  return {
    ...(channel ? { channel } : {}),
    launchOptions: executablePath && !channel ? { executablePath } : {},
  };
}

// Which spec files each kind of project runs. Desktop projects run everything except the phone and sign-in
// specs; phone projects run the phone spec plus the accessibility and visual specs, which pick their
// screens by layout; sign-in projects run against the passcode server.
const DESKTOP_SPECS = { testIgnore: /(mobile|auth)\.spec/ };
const PHONE_SPECS = { testMatch: /(mobile|a11y|visual)\.spec/ };
const AUTH_SPECS = { testMatch: /auth\.spec/ };

type Engine = "chromium" | "firefox" | "webkit";
const all: (Project & { engine: Engine })[] = [
  // Chromium (the local default). Names kept from before so reports stay comparable.
  { engine: "chromium", name: "desktop", ...DESKTOP_SPECS, use: { ...chromiumOpts(), viewport: DESKTOP } },
  { engine: "chromium", name: "iphone-13", ...PHONE_SPECS, use: { ...device("iPhone 13", "chromium"), ...chromiumOpts() } },
  { engine: "chromium", name: "pixel-7", ...PHONE_SPECS, use: { ...device("Pixel 7"), ...chromiumOpts() } },
  { engine: "chromium", name: "pixel-7-landscape", testMatch: /mobile\.spec/, use: { ...device("Pixel 7 landscape"), ...chromiumOpts() } },
  { engine: "chromium", name: "auth-desktop", ...AUTH_SPECS, use: { ...chromiumOpts(), viewport: DESKTOP, baseURL: authURL } },
  { engine: "chromium", name: "auth-pixel-7", ...AUTH_SPECS, use: { ...device("Pixel 7"), ...chromiumOpts(), baseURL: authURL } },

  // Firefox: the desktop suite and sign-in. (Firefox has no mobile emulation in Playwright.)
  { engine: "firefox", name: "desktop-firefox", ...DESKTOP_SPECS, use: { ...device("Desktop Firefox"), viewport: DESKTOP } },
  { engine: "firefox", name: "auth-desktop-firefox", ...AUTH_SPECS, use: { ...device("Desktop Firefox"), viewport: DESKTOP, baseURL: authURL } },

  // WebKit: desktop Safari, iPhones in portrait and landscape, an iPad in both orientations.
  { engine: "webkit", name: "desktop-safari", ...DESKTOP_SPECS, use: { ...device("Desktop Safari"), viewport: DESKTOP } },
  { engine: "webkit", name: "iphone-14-webkit", ...PHONE_SPECS, use: device("iPhone 14") },
  { engine: "webkit", name: "iphone-13-webkit-landscape", testMatch: /mobile\.spec/, use: device("iPhone 13 landscape") },
  // 810 px wide in portrait: below the 900 px breakpoint, so the iPad gets the phone layout.
  { engine: "webkit", name: "ipad-webkit", testMatch: /mobile\.spec/, use: device("iPad (gen 7)") },
  // 1080 px in landscape: the desktop layout on a touch screen.
  { engine: "webkit", name: "ipad-webkit-landscape", testMatch: /app\.spec/, use: device("iPad (gen 7) landscape") },
  { engine: "webkit", name: "auth-iphone-14-webkit", ...AUTH_SPECS, use: { ...device("iPhone 14"), baseURL: authURL } },
];

const server = (p: string, env: Record<string, string>, url: string) => ({
  command: `"${python}" -m uvicorn app.api.server:app_factory --factory --host 127.0.0.1 --port ${p} --no-proxy-headers`,
  cwd: join(root, "backend"),
  url,
  reuseExistingServer: false,
  timeout: 60_000,
  env: { ...common, ...env },
});

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  // One worker: the replay tests switch the whole server into replay mode.
  workers: 1,
  // No retries anywhere: a flaky test is fixed (usually its wait), not retried.
  retries: 0,
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    // Visual comparisons (visual.spec.ts): tolerant of anti-aliasing; prices, clocks and charts are masked.
    toHaveScreenshot: { maxDiffPixelRatio: 0.02, threshold: 0.25, animations: "disabled", caret: "hide", scale: "css" },
  },
  // Baselines are per operating system and project (fonts and rendering differ). See e2e/README.md.
  snapshotPathTemplate: "{testDir}/__screenshots__/{platform}/{projectName}/{testFileName}/{arg}{ext}",
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: external || `http://127.0.0.1:${port}`,
    trace: process.env.CI ? "retain-on-failure" : "off",
    screenshot: "only-on-failure",
    viewport: DESKTOP,
  },
  projects: all.filter((p) => engines.has(p.engine)).map(({ engine: _e, ...p }) => p),
  webServer: [
    ...(external ? [] : [server(port!, { PAPERDESK_DATA: process.env.E2E_DATA! }, `http://127.0.0.1:${port}/api/status`)]),
    server(authPort!, { PAPERDESK_DATA: process.env.E2E_AUTH_DATA!, PAPERDESK_PASSCODE: process.env.E2E_PASSCODE!,
                        PAPERDESK_LOOPBACK_BYPASS: "0" }, `${authURL}/api/auth/state`),
  ],
});
