import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./fixtures";
import { screensFor, isPhone } from "./screens";

// axe-core on every key screen, in the desktop layout (desktop projects) and the phone layout (phone
// projects). Serious and critical violations fail the test; moderate and minor ones are attached to the
// report so they stay visible without blocking.

const authURL = `http://127.0.0.1:${process.env.E2E_AUTH_PORT}`;

async function scan(page: import("@playwright/test").Page, where: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const rest = results.violations.filter((v) => !blocking.includes(v));
  if (rest.length) await test.info().attach(`${where}: other axe findings`, { body: JSON.stringify(rest.map((v) => ({ id: v.id, impact: v.impact, targets: v.nodes.map((n) => n.target.join(" ")) })), null, 2), contentType: "application/json" });
  return blocking.map((v) => `${where}: [${v.impact}] ${v.id}: ${v.help} -> ${v.nodes.slice(0, 5).map((n) => n.target.join(" ")).join(" | ")}`);
}

test("key screens have no serious or critical accessibility violations", async ({ page, open }) => {
  const found: string[] = [];
  for (const screen of screensFor(page)) {
    await open();
    await screen.show(page);
    found.push(...await scan(page, `${isPhone(page) ? "phone" : "desktop"} ${screen.name}`));
  }
  expect(found, "serious or critical axe violations").toEqual([]);
});

test.describe("sign-in screen", () => {
  test.use({ baseURL: authURL });
  test("has no serious or critical accessibility violations", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByLabel("Passcode")).toBeVisible();
    // (A wrong passcode is not tried here: failed sign-ins slow down later ones from the same address.)
    expect(await scan(page, "login")).toEqual([]);
  });
});
