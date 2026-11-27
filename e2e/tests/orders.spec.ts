import { test, expect, createAccount, lastPrice, ticket, bottomTab, review, place } from "./fixtures";
import type { Page } from "@playwright/test";

// Order types and linked orders in the ticket, and how the Orders tab shows them (desktop).

const orderRows = (page: Page) => page.locator(".bottom tbody tr");
const fmtTime = (page: Page, ts: number) => page.evaluate((t) => new Date(t * 1000).toLocaleString([], {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }), ts);

test("a market-on-close order waits for the close and shows its auction time", async ({ page, open, request, account }) => {
  await open();
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("market_on_close");
  // The one-line explanation is the preview's own text: in this session mode the close is UTC midnight.
  const hint = t.locator(".ticket-hint").first();
  await expect(hint).toContainText("waits for the next market close");
  await expect(hint).toContainText("never closes, so that is the next UTC midnight");
  await expect(t.getByLabel("Time in force")).toHaveCount(0);
  await review(page, "3");
  await expect(t.locator(".preview")).toContainText("next UTC midnight");
  await place(page);
  await expect(page.getByText("Order placed: buy 3 MKTX (working)")).toBeVisible();

  const [o] = await (await request.get(`/api/accounts/${account.id}/orders`)).json();
  expect(o.type).toBe("market_on_close");
  expect(o.auction_at % 86400).toBe(0);
  await bottomTab(page, /^Orders/).click();
  const row = orderRows(page).filter({ hasText: "Market on close" });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Working");
  await expect(row).toContainText(`At the close · ${await fmtTime(page, o.auction_at)}`);
  await expect(row).toContainText("closing price");
});

test("a good-till-date limit order shows its date; a past date shows the server's reason", async ({ page, open, request, problems }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await t.getByLabel("Time in force").selectOption("gtd");
  const date = t.getByLabel("Good till date", { exact: true });
  await expect(date).toHaveAttribute("type", "date");

  await date.fill("2020-01-02");
  await review(page, "4");
  await expect(t.getByRole("alert")).toHaveText("good-till date has already passed");
  // The refused preview is a 422 response, which the browser logs; that one is expected here.
  const i = problems.findIndex((p) => p.includes("status of 422"));
  if (i >= 0) problems.splice(i, 1);

  const day = new Date(Date.now() + 10 * 86400000).toISOString().slice(0, 10);
  await date.fill(day);
  await t.getByRole("button", { name: /^Review/ }).click();
  await expect(t.locator(".preview")).toContainText(`expires at the close on ${day}`);
  await place(page);
  await bottomTab(page, /^Orders/).click();
  const row = orderRows(page).filter({ hasText: `Good till ${day}` });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Working");
});

test("one triggers other: two follow-on orders are held, then work once the entry fills", async ({ page, open, request, account }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.9).toFixed(2));
  await t.getByLabel("Time in force").selectOption("gtc");
  await t.getByLabel("Linked orders").selectOption("oto");
  await expect(t.getByText("One triggers other (OTO)")).toBeVisible();
  const first = t.getByRole("group", { name: "Follow-on order 1" });
  await expect(first.getByLabel("Side")).toHaveValue("sell");
  await first.getByLabel("Limit price").fill((px * 1.5).toFixed(2));
  await t.getByRole("button", { name: "Add a follow-on order" }).click();
  const second = t.getByRole("group", { name: "Follow-on order 2" });
  await second.getByLabel("Order type").selectOption("stop");
  await second.getByLabel("Stop price").fill((px * 0.5).toFixed(2));
  await t.getByLabel("Cancel the others when one fills").check();
  await review(page, "10");
  const created = t.getByRole("region", { name: "Orders that will be created" });
  await expect(created.locator("li")).toHaveCount(3);
  await expect(created.locator("li").nth(0)).toContainText("Entry");
  await expect(created.locator("li").nth(1)).toContainText(`Sell the filled quantity of MKTX, limit ${(px * 1.5).toFixed(2)}, good till canceled`);
  await expect(created.locator("li").nth(2)).toContainText(`stop ${(px * 0.5).toFixed(2)}`);
  await place(page);
  await expect(page.getByText(/Order placed: buy 10 MKTX \(working\), with 2 linked orders/)).toBeVisible();

  await bottomTab(page, /^Orders/).click();
  await expect(page.locator("tr.og-head")).toContainText("One triggers other, follow-on orders cancel each other · 3 orders");
  const members = page.locator("tr.og-member");
  await expect(members).toHaveCount(3);
  await expect(members.nth(0)).toContainText("Entry");
  await expect(members.nth(0)).toContainText("Working");
  for (const n of [1, 2]) {
    await expect(members.nth(n)).toContainText("Exit, after the entry fills");
    await expect(members.nth(n)).toContainText("Held · waits for the entry");
  }

  // Move the entry's limit above the market so it fills; the follow-on orders are then released.
  const orders = await (await request.get(`/api/accounts/${account.id}/orders`)).json();
  const entry = orders.find((o: any) => !o.parent_id);
  const r = await request.patch(`/api/accounts/${account.id}/orders/${entry.id}`, { data: { limit_price: (px * 1.1).toFixed(2) } });
  expect(r.ok()).toBeTruthy();
  await expect(members.nth(0)).toContainText("Filled", { timeout: 15_000 });
  await expect(members.nth(1)).toContainText("Working");
  await expect(members.nth(2)).toContainText("Working");
  await expect(members.nth(1)).toContainText("10");
});

test("one cancels the other: a group of two orders works together", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const tecx = await lastPrice(request, "TECX");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.8).toFixed(2));
  await t.getByLabel("Linked orders").selectOption("oco");
  await expect(t.getByText("One cancels the other (OCO)")).toBeVisible();
  const other = t.getByRole("group", { name: "Order 2" });
  await other.getByLabel("Symbol").fill("TECX");
  await other.getByLabel("Quantity").fill("5");
  await other.getByLabel("Limit price").fill((tecx * 0.8).toFixed(2));
  await other.getByLabel("Time in force").selectOption("day");
  await review(page, "2");
  const created = t.getByRole("region", { name: "Orders that will be created" });
  await expect(created.locator("li")).toHaveCount(2);
  await expect(created.locator("li").nth(1)).toContainText(`Buy 5 TECX, limit ${(tecx * 0.8).toFixed(2)}`);
  await expect(t.locator(".preview")).toContainText("One-cancels-other");
  await place(page);
  await expect(page.getByText("One-cancels-other group placed: 2 orders (working, working)")).toBeVisible();
  await bottomTab(page, /^Orders/).click();
  await expect(page.locator("tr.og-head")).toContainText("One cancels the other · 2 orders");
  await expect(page.locator("tr.og-member").filter({ hasText: "Order 1 of 2" })).toContainText("MKTX");
  await expect(page.locator("tr.og-member").filter({ hasText: "Order 2 of 2" })).toContainText("TECX");
  await expect(page.locator("tr.og-member").filter({ hasText: "Working" })).toHaveCount(2);
});

test("linked orders stop at the API's limit of five orders", async ({ page, open }) => {
  await open();
  const t = ticket(page);
  await t.getByLabel("Linked orders").selectOption("oto");
  for (let i = 0; i < 3; i++) await t.getByRole("button", { name: "Add a follow-on order" }).click();
  await expect(t.getByRole("button", { name: "At most 4 follow-on orders" })).toBeDisabled();
  await t.getByLabel("Linked orders").selectOption("oco");
  for (let i = 0; i < 3; i++) await t.getByRole("button", { name: "Add an order to the group" }).click();
  await expect(t.getByRole("button", { name: "At most 5 orders in a group" })).toBeDisabled();
  // The bracket shortcut is still there for a plain order.
  await t.getByLabel("Linked orders").selectOption("none");
  await expect(t.getByLabel("Attach take profit and stop loss")).toBeVisible();
});
