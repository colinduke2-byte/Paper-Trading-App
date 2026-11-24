import { test, expect, createAccount, lastPrice, ticket, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";
import { builder, checkBullCall, fact, inputFontSizes, isPreview, openBuilder, overflow, smallTargets } from "./strategyHelpers";

// The new order kinds and the strategy builder in the phone layout (iPhone 13 and Pixel 7 profiles).
// Every interaction is a tap, so a covered control fails instead of being clicked through.

const navButton = (page: Page, view: string) => page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: view, exact: true });
async function go(page: Page, view: string) {
  await navButton(page, view).tap();
  await expect(navButton(page, view)).toHaveAttribute("aria-current", "page");
}

test("market-on-close and good-till-date orders from the Trade view", async ({ page, open, request, account }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await go(page, "Trade");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("market_on_close");
  await expect(t.locator(".ticket-hint").first()).toContainText("next UTC midnight");
  await t.getByLabel("Shares").fill("2");
  await t.getByRole("button", { name: /^Review/ }).tap();
  await t.getByRole("button", { name: "Place paper order" }).tap();
  await expect(page.getByText("Order placed: buy 2 MKTX (working)")).toBeVisible();

  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await t.getByLabel("Time in force").selectOption("gtd");
  const date = t.getByLabel("Good till date", { exact: true });
  const fontSize = await date.evaluate((e) => parseFloat(getComputedStyle(e).fontSize));
  expect(fontSize).toBeGreaterThanOrEqual(16);
  const day = new Date(Date.now() + 9 * 86400000).toISOString().slice(0, 10);
  await date.fill(day);
  await t.getByLabel("Shares").fill("1");
  await t.getByRole("button", { name: /^Review/ }).tap();
  await t.getByRole("button", { name: "Place paper order" }).tap();
  await expect(page.getByText("Order placed: buy 1 MKTX (working)")).toBeVisible();

  const orders = await (await request.get(`/api/accounts/${account.id}/orders`)).json();
  const moc = orders.find((o: any) => o.type === "market_on_close");
  const when = await page.evaluate((ts) => new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }), moc.auction_at);
  await go(page, "Account");
  await bottomTab(page, /^Orders/).tap();
  await expect(page.locator(".bottom tbody tr").filter({ hasText: "Market on close" })).toContainText(`At the close · ${when}`);
  await expect(page.locator(".bottom tbody tr").filter({ hasText: `Good till ${day}` })).toHaveCount(1);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
});

test("linked orders on a phone: touch targets, numeric keyboards, one cancels the other", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await go(page, "Trade");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.8).toFixed(2));
  await t.getByLabel("Linked orders").selectOption("oto");
  await t.getByRole("button", { name: "Add a follow-on order" }).tap();
  await t.getByLabel("Cancel the others when one fills").tap();
  const found = await smallTargets(t, "ticket, one triggers other");
  for (const s of await inputFontSizes(t)) expect(s, "input font size").toBeGreaterThanOrEqual(16);
  const fo = t.getByRole("group", { name: "Follow-on order 1" });
  await expect(fo.getByLabel("Quantity")).toHaveAttribute("inputmode", "decimal");
  await expect(fo.getByLabel("Limit price")).toHaveAttribute("inputmode", "decimal");
  expect(await overflow(page)).toBeLessThanOrEqual(0);

  await t.getByLabel("Linked orders").selectOption("oco");
  const other = t.getByRole("group", { name: "Order 2" });
  await other.getByLabel("Quantity").fill("3");
  await other.getByLabel("Limit price").fill((px * 0.7).toFixed(2));
  found.push(...await smallTargets(t, "ticket, one cancels the other"));
  await t.getByLabel("Shares").fill("3");
  await t.getByRole("button", { name: /^Review/ }).tap();
  await expect(t.getByRole("region", { name: "Orders that will be created" }).locator("li")).toHaveCount(2);
  found.push(...await smallTargets(t, "ticket, review"));
  await t.getByRole("button", { name: "Place paper order" }).tap();
  await expect(page.getByText("One-cancels-other group placed: 2 orders (working, working)")).toBeVisible();
  await go(page, "Account");
  await bottomTab(page, /^Orders/).tap();
  await expect(page.locator("tr.og-head")).toContainText("One cancels the other · 2 orders");
  await expect(page.locator(".og-label")).toBeInViewport();
  found.push(...await smallTargets(page.locator(".bottom"), "Orders tab"));
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  expect(found, "controls smaller than 40 px").toEqual([]);
});

test("strategy builder is a full-screen sheet: bull call spread placed, grouped and closed", async ({ page, open }) => {
  await open();
  const d = await openBuilder(page, true);
  const v = page.viewportSize()!;
  const box = (await d.boundingBox())!;
  expect(Math.round(box.x)).toBe(0);
  expect(Math.round(box.y)).toBe(0);
  expect(Math.round(box.width)).toBe(v.width);
  expect(Math.round(box.height)).toBe(v.height);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  const found = await smallTargets(d, "builder");
  for (const s of await inputFontSizes(d)) expect(s, "input font size").toBeGreaterThanOrEqual(16);
  await expect(d.getByLabel("Quantity (units)")).toHaveAttribute("inputmode", "numeric");
  await expect(d.locator(".sb-leg").first().getByLabel("Ratio")).toHaveAttribute("inputmode", "numeric");
  await expect(d.getByLabel("Net limit price")).toHaveAttribute("inputmode", "decimal");
  // The leg editor and order fields fit the width: nothing inside the sheet scrolls sideways.
  expect(await d.locator(".sb-body").evaluate((e) => e.scrollWidth - e.clientWidth)).toBeLessThanOrEqual(0);

  await d.getByLabel("Order type").selectOption("market");
  const [res] = await Promise.all([page.waitForResponse(isPreview), d.getByRole("button", { name: "Review order" }).tap()]);
  const pre = await res.json();
  await checkBullCall(page, pre, 1);
  await expect(fact(page, "Maximum loss")).toBeInViewport();
  found.push(...await smallTargets(d, "builder, review"));
  expect(await d.locator(".sb-body").evaluate((e) => e.scrollWidth - e.clientWidth)).toBeLessThanOrEqual(0);
  await d.getByRole("button", { name: "Place paper order" }).tap();
  await expect(d).toHaveCount(0);
  await expect(page.getByText(/^Filled: Bull call spread, 1 × MKTX/)).toBeVisible();

  await go(page, "Account");
  await bottomTab(page, /^Positions/).tap();
  const group = page.locator("tr.pos-group");
  await expect(group).toContainText("Bull call spread · MKTX");
  const close = group.getByRole("button", { name: "Close strategy…" });
  await expect(close).toBeInViewport();
  found.push(...await smallTargets(page.locator(".bottom"), "Positions tab"));
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await close.tap();
  const c = builder(page);
  await expect(c.locator(".sb-leg").nth(0).getByLabel("Side")).toHaveValue("sell");
  await c.getByLabel("Order type").selectOption("market");
  await c.getByRole("button", { name: "Review order" }).tap();
  await c.getByRole("button", { name: "Place paper order" }).tap();
  await expect(page.getByText(/^Filled: Bear call spread, 1 × MKTX/)).toBeVisible();
  await expect(page.getByText("No open positions.")).toBeVisible();
  expect(found, "controls smaller than 40 px").toEqual([]);
});

test("chain add-to-strategy mode by tapping, and a cash account refusal", async ({ page, open, request }) => {
  const cash = await createAccount(request, "cash", "100000");
  await open(cash);
  await page.getByRole("tab", { name: "Options" }).tap();
  await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
  await page.getByRole("button", { name: "Add to strategy" }).tap();
  const rows = page.locator(".chain-table tbody tr");
  const mid = Math.floor((await rows.count()) / 2);
  await rows.nth(mid).getByTitle("Add a sell leg").first().tap();
  await rows.nth(mid + 2).getByTitle("Add a buy leg").first().tap();
  const tray = page.getByRole("region", { name: "Strategy legs picked" });
  await expect(tray).toContainText(/^Sell MKTX .* Call · Buy MKTX .* Call/);
  // Picking does not open the ticket.
  await expect(navButton(page, "Chart")).toHaveAttribute("aria-current", "page");
  await expect(tray.getByRole("button", { name: /^Open in strategy builder/ })).toBeInViewport();
  expect(await smallTargets(tray, "tray")).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await tray.getByRole("button", { name: "Open in strategy builder (2 legs)" }).tap();
  const d = builder(page);
  await expect(d.locator(".sb-name")).toHaveText("Bear call spread");
  const [res] = await Promise.all([page.waitForResponse(isPreview), d.getByRole("button", { name: "Review order" }).tap()]);
  const pre = await res.json();
  expect(pre.ok).toBe(false);
  await expect(d.getByRole("alert")).toHaveText(`This order would be rejected: ${pre.reason}.`);
  await expect(d.getByRole("alert")).toBeInViewport();
  await d.getByRole("button", { name: "Close", exact: true }).tap();
  await expect(d).toHaveCount(0);
});
