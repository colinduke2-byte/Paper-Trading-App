import { expect, type Page } from "@playwright/test";
import { ticket, bottomTab } from "./fixtures";

// The key screens, shared by the accessibility (a11y.spec.ts) and visual (visual.spec.ts) checks. Each one
// opens from the page that `open()` leaves (MKTX on the Chart view) and waits until it has settled.

export type Screen = { name: string; show: (page: Page) => Promise<void> };

export const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1440) < 900;

const nav = (page: Page, view: string) =>
  page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: view, exact: true });

async function phoneView(page: Page, view: string) {
  await nav(page, view).click();
  await expect(nav(page, view)).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("region", { name: `${view} view` })).toBeVisible();
}

async function chartReady(page: Page) {
  await expect(page.getByRole("figure", { name: /price chart$/ }).locator("canvas").first()).toBeVisible();
}

async function reviewed(page: Page) {
  const t = ticket(page);
  await t.getByLabel("Shares").fill("1");
  await t.getByRole("button", { name: /^Review/ }).click();
  await expect(t.getByRole("button", { name: "Place paper order" })).toBeVisible();
}

async function options(page: Page) {
  await page.getByRole("tab", { name: "Options" }).click();
  await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
}

async function reports(page: Page) {
  await bottomTab(page, "Reports").click();
  await expect(page.locator(".bottom")).toContainText("Returns and risk");
}

async function settings(page: Page) {
  if (isPhone(page)) {
    await page.getByRole("button", { name: "Menu" }).click();
    await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Account rules" }).click();
  } else {
    await page.getByRole("button", { name: "Account rules", exact: true }).click();
  }
  await expect(page.getByRole("dialog", { name: "Account rules" })).toBeVisible();
  // The account figures and the rules load separately after the dialog opens.
  const d = page.getByRole("dialog", { name: "Account rules" });
  await expect(d.getByText("Settled cash")).toBeVisible();
  await expect(d.getByRole("checkbox").first()).toBeVisible();
  await expect(d.getByText("Loading", { exact: false })).toHaveCount(0);
}

export const DESKTOP_SCREENS: Screen[] = [
  { name: "main", show: chartReady },
  { name: "ticket-review", show: reviewed },
  { name: "options", show: options },
  { name: "reports", show: reports },
  { name: "settings", show: settings },
  { name: "new-account", show: async (page) => {
    await page.getByRole("button", { name: "New account" }).click();
    await expect(page.getByRole("dialog")).toContainText("New paper account");
  } },
];

export const PHONE_SCREENS: Screen[] = [
  { name: "watchlist", show: async (page) => { await phoneView(page, "Watchlist"); } },
  { name: "chart", show: async (page) => { await phoneView(page, "Chart"); await chartReady(page); } },
  { name: "trade", show: async (page) => { await phoneView(page, "Trade"); } },
  { name: "trade-review", show: async (page) => { await phoneView(page, "Trade"); await reviewed(page); } },
  { name: "account", show: async (page) => { await phoneView(page, "Account"); } },
  { name: "options", show: async (page) => { await phoneView(page, "Chart"); await options(page); } },
  { name: "reports", show: async (page) => { await phoneView(page, "Account"); await reports(page); } },
  { name: "menu", show: async (page) => {
    await page.getByRole("button", { name: "Menu" }).click();
    await expect(page.getByRole("dialog", { name: "Menu" })).toBeVisible();
  } },
  { name: "settings", show: settings },
];

export const screensFor = (page: Page) => (isPhone(page) ? PHONE_SCREENS : DESKTOP_SCREENS);
