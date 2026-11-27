import { test, expect, confirmDialog } from "./fixtures";
import type { Page } from "@playwright/test";

// UX review follow-ups in the phone shell (docs/reviews/UX_REVIEW_2026-11-24.md).

const nav = (page: Page, name: string) => page.getByRole("navigation", { name: "Views" }).getByRole("button", { name });

test("an Info popover explains a term on tap and fits the screen", async ({ page, open }) => {
  await open();
  await nav(page, "Account").tap();
  const btn = page.locator(".m-acct").getByRole("button", { name: "What is Buying power?" });
  const box = (await btn.boundingBox())!;
  expect(box.width, "tap target width").toBeGreaterThanOrEqual(40);
  expect(box.height, "tap target height").toBeGreaterThanOrEqual(40);
  await btn.tap();
  const pop = page.getByRole("dialog", { name: "Buying power" });
  await expect(pop).toBeVisible();
  await expect(pop).toContainText("How much stock you can buy right now");
  const p = (await pop.boundingBox())!;
  const vw = page.viewportSize()!.width;
  expect(p.x, "popover starts inside the screen").toBeGreaterThanOrEqual(0);
  expect(p.x + p.width, "popover ends inside the screen").toBeLessThanOrEqual(vw + 1);
  await page.locator(".m-acct-name").tap(); // a tap elsewhere closes it
  await expect(pop).toHaveCount(0);
});

test("the menu keeps Reset account apart and the last view is remembered", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Menu" }).tap();
  const sheet = page.getByRole("dialog", { name: "Menu" });
  await expect(sheet.locator(".m-sheet-actions").getByRole("button", { name: "Reset account" })).toHaveCount(0);
  await expect(sheet.locator(".m-sheet-danger").getByRole("button", { name: "Reset account" })).toBeVisible();
  await sheet.getByRole("button", { name: "Close" }).tap();
  await nav(page, "Account").tap();
  await page.reload();
  await expect(nav(page, "Account")).toHaveAttribute("aria-current", "page");
});

test("toasts appear above the tab bar, clear of the account figures", async ({ page, open }) => {
  await open();
  await nav(page, "Account").tap();
  // A rule change produces a toast without placing an order.
  await page.getByRole("button", { name: "Menu" }).tap();
  await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Account rules" }).tap();
  const dlg = page.getByRole("dialog", { name: /Account rules/ });
  await dlg.getByRole("button", { name: "Restore default rules" }).tap();
  await confirmDialog(page, "Restore default rules", true);
  const toast = page.locator(".toast").filter({ hasText: "Default rules restored." });
  await expect(toast).toBeVisible();
  await dlg.getByRole("button", { name: "Close" }).tap();
  const t = (await toast.boundingBox())!;
  const top = (await page.locator(".m-top").boundingBox())!;
  const bar = (await page.getByRole("navigation", { name: "Views" }).boundingBox())!;
  expect(t.y, "toast below the top bar").toBeGreaterThan(top.y + top.height);
  if (bar.height > bar.width) {  // phone landscape: the views bar is a rail at the side, so stay clear of it
    expect(t.x, "toast clear of the side rail").toBeGreaterThanOrEqual(bar.x + bar.width - 1);
    expect(t.y + t.height, "toast inside the screen").toBeLessThanOrEqual(page.viewportSize()!.height + 1);
  } else {
    expect(t.y + t.height, "toast above the tab bar").toBeLessThanOrEqual(bar.y + 1);
  }
});
