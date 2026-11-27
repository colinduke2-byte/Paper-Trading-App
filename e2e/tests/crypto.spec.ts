import { test, expect, createAccount, ticket, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";

// Crypto on a weekend: the server's clock starts on Saturday 2026-11-28 with US stock sessions, so stocks are
// closed while the fictional coins trade. Runs on desktop and on an iPhone 13 profile (playwright.config.ts).

const phone = (page: Page) => (page.viewportSize()?.width ?? 1440) < 900;
const tap = async (page: Page, el: ReturnType<Page["locator"]>) => (phone(page) ? el.tap() : el.click());

async function view(page: Page, name: "Watchlist" | "Chart" | "Trade" | "Account") {
  if (!phone(page)) return;
  const b = page.getByRole("navigation", { name: "Views" }).getByRole("button", { name, exact: true });
  await b.tap();
  await expect(b).toHaveAttribute("aria-current", "page");
}

async function noSideScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
}

test("the stock market is closed but crypto trades: buy, limit order, sell and report", async ({ page, request, account, open }) => {
  const status = await (await request.get("/api/status")).json();
  expect(status.session).toBe("closed");
  expect(new Date(status.time * 1000).getUTCDay()).toBe(6); // Saturday

  await open(account, "MKTX");
  // Find a coin with the asset-class filter in the watchlist search.
  await view(page, "Watchlist");
  await tap(page, page.getByRole("group", { name: "Asset class" }).getByRole("button", { name: "Crypto" }));
  const results = page.locator(".results");
  await expect(results.getByRole("button", { name: /SIMCOIN\/USD/ })).toBeVisible();
  await expect(results.getByRole("button", { name: /SIMDUST\/USD/ })).toBeVisible();
  await expect(results.getByRole("button", { name: /MKTX/ })).toHaveCount(0);
  await tap(page, results.getByRole("button", { name: /SIMCOIN\/USD/ }));
  await expect(page.getByRole("heading", { name: "SIMCOIN/USD", level: 2 })).toBeVisible();
  await expect(page.getByText("Crypto · trades 24/7")).toBeVisible();

  // Market buy by quantity.
  await view(page, "Trade");
  const t = ticket(page);
  await expect(t.getByText("Crypto · 24/7")).toBeVisible();
  await t.getByLabel("Quantity in SIMCOIN").fill("0.0125");
  await tap(page, t.getByRole("button", { name: "Review: Buy SIMCOIN" }));
  await expect(t.getByText("Estimated fee (0.25%)")).toBeVisible();
  await expect(t.getByText("Crypto trades 24 hours a day")).toHaveCount(0); // GTC is the default, so no day-order note
  await tap(page, t.getByRole("button", { name: "Place paper order" }));
  await expect(page.locator(".toast").filter({ hasText: "Filled: buy 0.0125 SIMCOIN/USD" })).toBeVisible();
  await noSideScroll(page);

  // A limit order far below the market rests.
  const last = (await (await request.get("/api/quotes?symbols=SIMCOIN/USD")).json())["SIMCOIN/USD"].last;
  const limit = (Math.floor(last * 50) / 100).toFixed(2);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill(limit);
  await t.getByLabel("Quantity in SIMCOIN").fill("0.002");
  await tap(page, t.getByRole("button", { name: "Review: Buy SIMCOIN" }));
  await tap(page, t.getByRole("button", { name: "Place paper order" }));
  await expect(page.locator(".toast").filter({ hasText: "Order placed: buy 0.002 SIMCOIN/USD (working)" })).toBeVisible();

  // The position shows with its precision and a crypto tag.
  await view(page, "Account");
  await tap(page, bottomTab(page, /^Positions/));
  const row = page.locator(".bottom tr", { hasText: "SIMCOIN/USD" });
  await expect(row).toContainText("0.0125");
  await expect(row.locator(".tag")).toHaveText("Crypto");
  await tap(page, bottomTab(page, /^Orders/));
  await expect(page.locator(".bottom tr", { hasText: "SIMCOIN/USD" }).filter({ hasText: `limit $${Number(limit).toLocaleString("en-US", { minimumFractionDigits: 2 })}` })).toContainText("Working");

  // Sell it all (selling more than held is refused with the reason).
  await view(page, "Trade");
  await tap(page, t.getByRole("button", { name: "Sell", exact: true }));
  await t.getByLabel("Order type").selectOption("market");
  const short = await request.post(`/api/accounts/${account.id}/orders/preview`, { data: { symbol: "SIMCOIN/USD", side: "sell", qty: "1" } });
  expect(short.status()).toBe(422);
  expect((await short.json()).detail).toBe("crypto cannot be sold short: you hold 0.0125");
  await t.getByLabel("Quantity in SIMCOIN").fill("0.0125");
  await tap(page, t.getByRole("button", { name: "Review: Sell SIMCOIN" }));
  await tap(page, t.getByRole("button", { name: "Place paper order" }));
  await expect(page.locator(".toast").filter({ hasText: "Filled: sell 0.0125 SIMCOIN/USD" })).toBeVisible();

  // The realised-gains report includes the closed crypto lot.
  await view(page, "Account");
  await tap(page, bottomTab(page, "Reports"));
  await tap(page, page.getByRole("group", { name: "Report" }).getByRole("button", { name: "Gains" }));
  await expect(page.locator(".rep-table tr", { hasText: "SIMCOIN/USD" })).toContainText("0.0125");
  const csv = await (await request.get(`/api/accounts/${account.id}/export/fills.csv`)).text();
  expect(csv).toMatch(/SIMCOIN\/USD,SIMCOIN\/USD,buy,0\.0125,/);
  await noSideScroll(page);
});

test("buy a small-priced coin by dollar amount in a cash account", async ({ page, request, open }) => {
  const cash = await createAccount(request, "cash", "1000");
  await open(cash, "SIMDUST/USD");
  await expect(page.locator(".quote-head strong")).toHaveText(/^\$0\.00\d{4,}/); // a price under a cent keeps its decimals
  await view(page, "Trade");
  const t = ticket(page);
  await tap(page, t.getByRole("button", { name: "Amount (USD)" }));
  await t.getByLabel("Amount in US dollars").fill("50");
  await tap(page, t.getByRole("button", { name: "Review: Buy SIMDUST" }));
  await expect(t.locator(".preview")).toContainText("SIMDUST");
  await tap(page, t.getByRole("button", { name: "Place paper order" }));
  await expect(page.locator(".toast").filter({ hasText: "Filled: buy" })).toBeVisible();
  const s = await (await request.get(`/api/accounts/${cash.id}`)).json();
  expect(s.cash).toBeGreaterThan(949.8);
  expect(s.cash).toBeLessThan(950.01);
  expect(s.unsettled_cash).toBe(0);
  const pos = await (await request.get(`/api/accounts/${cash.id}/positions`)).json();
  expect(pos[0].symbol).toBe("SIMDUST/USD");
  expect(pos[0].kind).toBe("crypto");
  await noSideScroll(page);
  if (phone(page)) {
    // Touch targets in the ticket are at least 40 px.
    const small = await t.evaluate((root) => [...root.querySelectorAll<HTMLElement>("button, select, input")]
      .map((el) => el.getBoundingClientRect()).filter((r) => r.width && (r.width < 40 || r.height < 40)).length);
    expect(small).toBe(0);
    expect(await t.getByLabel("Amount in US dollars").evaluate((el) => getComputedStyle(el).fontSize)).toBe("16px");
  }
});
