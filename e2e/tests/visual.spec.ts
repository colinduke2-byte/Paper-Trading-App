import { join } from "node:path";
import { test, expect } from "./fixtures";
import { isPhone, screensFor } from "./screens";

// Visual regression: one screenshot per key screen, per project (engine and device), compared with the
// baseline in tests/__screenshots__/<platform>/<project>/. Prices, sizes, clocks, account names, dates and the
// canvas charts change on every run, so visual.css hides them in place while the screenshot is taken; the
// chart canvases are never compared. The account has a fixed name so the top bar always wraps the same way. The tolerance (playwright.config.ts) absorbs anti-aliasing.
//
// Baselines are made on the CI runner (ubuntu-latest), because fonts and rendering differ between machines.
// They are only checked on Linux; elsewhere (the Windows job, a Mac) this spec is skipped. How to update them:
// e2e/README.md.

test.skip(process.platform !== "linux", "visual baselines are made on Linux (the CI runner)");
// Locally a baseline from the CI runner rarely matches pixel for pixel, so the check runs in CI or on request.
test.skip(!process.env.CI && !process.env.E2E_VISUAL, "visual comparisons run in CI, or locally with E2E_VISUAL=1");

const authURL = `http://127.0.0.1:${process.env.E2E_AUTH_PORT}`;

const stylePath = join(__dirname, "visual.css");

test("key screens match their visual baselines", async ({ page, open, request }) => {
  test.setTimeout(120_000);
  const r = await request.post("/api/accounts", { data: { name: "Visual check", type: "margin", starting_cash: "100000" } });
  expect(r.ok()).toBeTruthy();
  const account = { id: (await r.json()).id, name: "Visual check" };
  for (const screen of screensFor(page)) {
    await open(account);
    await screen.show(page);
    await page.evaluate(() => document.fonts.ready);
    await expect.soft(page, `${isPhone(page) ? "phone" : "desktop"} ${screen.name}`).toHaveScreenshot(`${screen.name}.png`, { stylePath });
  }
});

test.describe("sign-in screen", () => {
  test.use({ baseURL: authURL });
  test("matches its visual baseline", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByLabel("Passcode")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await expect(page).toHaveScreenshot("login.png", { stylePath });
  });
});
