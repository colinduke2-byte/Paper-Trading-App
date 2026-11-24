import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";

// Chart state under passcode access (auth-desktop project; the second server has a passcode and no loopback
// bypass). Saves go through the session cookie and the Origin check, including the save sent while the page is
// being left (keepalive fetch on pagehide).

const PASSCODE = process.env.E2E_PASSCODE!;

async function signIn(page: Page) {
  await page.goto("/");
  await page.getByLabel("Passcode").fill(PASSCODE);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("combobox", { name: "Account" })).toBeVisible();
  await expect(page.locator(".ct-stage")).toHaveAttribute("data-loaded", "1");
}

test("chart settings and drawings save and reload when signed in", async ({ page, browser }) => {
  test.skip(page.viewportSize()!.width < 900, "drawing by touch is covered by chart.mobile.spec.ts");
  await signIn(page);
  expect((await page.request.delete("/api/chartstate", { headers: { origin: new URL(page.url()).origin } })).ok()).toBe(true);
  await page.reload();
  await expect(page.locator(".ct-stage")).toHaveAttribute("data-loaded", "1");

  await page.getByLabel("Chart type").selectOption("area");
  await page.getByRole("button", { name: "Draw", exact: true }).click();
  await page.getByRole("button", { name: "Horizontal line" }).click();
  const box = (await page.getByTestId("draw-layer").boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.getByRole("button", { name: /^Drawings \(1\)/ })).toBeVisible();
  // Reload straight away, inside the save delay: the pending save goes out as the page is left.
  await page.reload();
  await expect(page.locator(".ct-stage")).toHaveAttribute("data-loaded", "1");
  await expect(page.getByLabel("Chart type")).toHaveValue("area");

  // Another browser profile (no local cache) signs in and gets the same state from the server.
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: new URL(page.url()).origin });
  const other = await ctx.newPage();
  const errors: string[] = [];
  other.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await signIn(other);
  await expect(other.getByLabel("Chart type")).toHaveValue("area");
  await other.getByRole("button", { name: "Draw", exact: true }).click();
  await expect(other.getByRole("button", { name: /^Drawings \(1\)/ })).toBeVisible();
  await ctx.close();
  expect(errors).toEqual([]);

  // Signed out, the route is closed.
  const anon = await browser.newContext({ baseURL: new URL(page.url()).origin });
  expect((await anon.request.get("/api/chartstate")).status()).toBe(401);
  await anon.close();
  await page.request.delete("/api/chartstate", { headers: { origin: new URL(page.url()).origin } });
});
