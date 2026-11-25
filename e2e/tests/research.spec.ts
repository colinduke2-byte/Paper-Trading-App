import { test, expect, lastPrice } from "./fixtures";

// Simulated research on the synthetic feed: everything is labelled Simulated and comes from the seed.

test("research tab shows simulated news, earnings, dividends and key ratios", async ({ page, open }) => {
  await open(undefined, "NVLX");
  await page.getByRole("tab", { name: "Research" }).click();
  const view = page.locator(".view");
  await expect(view.getByRole("tab", { name: "NVLX" })).toHaveAttribute("aria-selected", "true");
  await expect(view.locator(".rs-source").first()).toContainText("Source: Simulated (fictional companies and events)");
  await expect(view.locator(".rs-news li").first()).toBeVisible();
  await expect(view.getByRole("heading", { name: "Earnings" })).toBeVisible();
  await expect(view.locator(".rs-list li").first()).toContainText("EPS est.");
  await expect(view.getByText(/Dividend 0\.0400 per share/).first()).toBeVisible();
  await expect(view.getByRole("heading", { name: "Key ratios" })).toBeVisible();
  await expect(view.getByText("Debt / equity")).toBeVisible();
});

test("financial statements switch between quarterly and annual", async ({ page, open }) => {
  await open(undefined, "ORBT");
  await page.getByRole("tab", { name: "Research" }).click();
  await page.locator(".view").getByRole("tab", { name: "Statements" }).click();
  const income = page.getByRole("region", { name: "Income statement" });
  await expect(income).toContainText("Revenue");
  await expect(income).toContainText("Earnings per share");
  await expect(income.locator("thead th").nth(1)).toHaveText(/^Q\d \d{4}$/);
  await expect(page.getByRole("region", { name: "Balance sheet" })).toContainText("Total assets");
  await expect(page.getByRole("region", { name: "Cash flow" })).toContainText("Free cash flow");
  await page.getByRole("button", { name: "Annual" }).click();
  await expect(income.locator("thead th").nth(1)).toHaveText(/^FY \d{4}$/);
});

test("market view: news filter, economic and earnings calendars, held symbols marked", async ({ page, open, request, account }) => {
  const px = await lastPrice(request, "PXLR");
  const r = await request.post(`/api/accounts/${account.id}/orders`, { data: { symbol: "PXLR", side: "buy", qty: 1, type: "limit", limit_price: Math.round(px * 1.05 * 100) / 100 } });
  expect(r.ok()).toBeTruthy();
  await open(undefined, "MKTX");
  await page.getByRole("tab", { name: "Research" }).click();
  await page.locator(".view").getByRole("tab", { name: "Market and calendars" }).click();
  await expect(page.getByRole("heading", { name: "Economic calendar" })).toBeVisible();
  await expect(page.locator(".rs-econ")).toContainText("Central bank rate decision");
  await expect(page.locator(".rs-econ")).toContainText("Jobs report");
  const earnings = page.locator("section[aria-labelledby=rs-earn]");
  await expect(earnings.locator("li").first()).toContainText("EPS est.");
  await expect(earnings.locator("li", { hasText: "PXLR" }).first()).toContainText("held");
  await page.getByLabel("News category").selectOption("rating");
  await expect(page.locator("section[aria-labelledby=rs-mnews] .rs-cat").first()).toHaveText("Analyst");
  // picking a symbol from the earnings calendar selects it
  await earnings.getByRole("button", { name: "AERO" }).first().click();
  await expect(page.getByRole("heading", { name: "AERO", level: 2 })).toBeVisible();
});

test("company tab shows ratios from the statements, and fund holdings for a fund", async ({ page, open }) => {
  await open(undefined, "UTIL");
  await page.getByRole("tab", { name: "Company" }).click();
  await expect(page.getByText("Every company in this build is fictional")).toBeVisible();
  await expect(page.getByRole("heading", { name: /Key ratios \(Q\d \d{4}, trailing four quarters\)/ })).toBeVisible();
  await expect(page.locator(".company-ratios")).toContainText("Payout ratio");
  await page.locator(".watch").getByRole("cell", { name: "TECX", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Fund details" })).toBeVisible();
  await expect(page.locator(".company-ratios")).toContainText("Sector weights");
  await expect(page.locator(".company-ratios")).toContainText("Technology");
});
