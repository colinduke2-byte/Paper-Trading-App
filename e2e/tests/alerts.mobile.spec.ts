import { test, expect, lastPrice, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";

// Alerts on the phone layout. Runs on one phone profile (Pixel 7); every interaction is a real tap.
test.beforeEach(() => test.skip(test.info().project.name !== "pixel-7", "one phone profile is enough for this flow"));

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function smallTargets(page: Page, scope: string): Promise<string[]> {
  return page.evaluate((sel) => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(`${sel} button, ${sel} select, ${sel} input:not([type=checkbox])`)) {
      const r = el.getBoundingClientRect();
      if (r.width && r.height && (r.width < 40 || r.height < 40)) out.push(`${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24)} ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    return out;
  }, scope);
}

test("phone: create a price alert, see it fire, the menu counts it, mark it read", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Account", exact: true }).tap();
  await bottomTab(page, "Alerts").tap();
  await page.getByLabel("Alert symbol").fill("MKTX");
  await page.getByLabel("Alert price").fill((px / 2).toFixed(2));
  const size = await page.getByLabel("Alert price").evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  expect(size).toBeGreaterThanOrEqual(16);
  await page.getByRole("button", { name: "Add price alert" }).tap();
  await expect(page.locator(".toasts")).toContainText("Alert: MKTX");
  expect(await overflow(page)).toBeLessThanOrEqual(0);

  await page.getByRole("button", { name: "Menu" }).tap();
  const menu = page.getByRole("dialog", { name: "Menu" });
  await menu.getByRole("button", { name: "Notifications (1 unread)" }).tap();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await expect(panel.locator(".notif.unread")).toHaveCount(1);
  expect(await smallTargets(page, ".notif-panel")).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await panel.getByRole("button", { name: "Delivery" }).tap();
  expect(await smallTargets(page, ".notif-panel")).toEqual([]);
  await panel.getByRole("button", { name: "Mark all read" }).tap();
  await expect(panel.locator(".notif.unread")).toHaveCount(0);
  await panel.getByRole("button", { name: "Close" }).tap();
  await page.getByRole("button", { name: "Menu" }).tap();
  await expect(menu.getByRole("button", { name: "Notifications", exact: true })).toBeVisible();
  await menu.getByRole("button", { name: "Close" }).tap();

  // The builder fits the phone and its controls are big enough to tap.
  await page.getByRole("button", { name: "New alert…" }).tap();
  const dlg = page.getByRole("dialog", { name: "New alert" });
  for (const kind of ["price", "indicator", "option", "account", "reminder", "calendar"]) {
    await dlg.getByLabel("Alert type").selectOption(kind);
    expect(await smallTargets(page, ".alert-builder"), kind).toEqual([]);
    expect(await overflow(page), kind).toBeLessThanOrEqual(0);
  }
  await dlg.getByLabel("Alert type").selectOption("reminder");
  await dlg.getByLabel("Remind me in (minutes)").fill("30");
  await dlg.getByLabel("Message").fill("Check the close");
  await dlg.getByRole("button", { name: "Create alert" }).tap();
  await expect(page.locator(".alerts-table")).toContainText("Reminder at");
});
