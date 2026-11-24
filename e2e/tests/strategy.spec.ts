import { test, expect, createAccount, bottomTab } from "./fixtures";
import { builder, checkBullCall, fact, isPreview, num, openBuilder, r2, strikeOf, usd } from "./strategyHelpers";

// Strategy builder for multi-leg option orders, and how its orders and positions are shown (desktop).

test("bull call spread from the template: preview figures, payoff, place at market, grouped, then closed", async ({ page, open }) => {
  await open();
  const d = await openBuilder(page);
  await expect(d.getByLabel("Strategy")).toHaveValue("bull_call");
  await expect(d.locator(".sb-name")).toHaveText("Bull call spread");
  await expect(d.locator("[data-leg-quote] .tag").first()).toHaveText("model");
  await d.getByLabel("Quantity (units)").fill("2");
  await d.getByLabel("Order type").selectOption("market");
  const [res] = await Promise.all([page.waitForResponse(isPreview), d.getByRole("button", { name: "Review order" }).click()]);
  const pre = await res.json();
  expect(pre.ok).toBe(true);
  await checkBullCall(page, pre, 2);
  await expect(d.locator(".sb-preview-legs tbody tr")).toHaveCount(2);
  await expect(d.locator(".sb-preview-legs .tag").first()).toHaveText("model");
  await d.getByRole("button", { name: "Place paper order" }).click();
  await expect(d).toHaveCount(0);
  await expect(page.getByText(/^Filled: Bull call spread, 2 × MKTX at net [\d.]+ debit$/)).toBeVisible();

  await bottomTab(page, /^Positions/).click();
  const group = page.locator("tr.pos-group");
  await expect(group).toHaveCount(1);
  await expect(group).toContainText("Bull call spread · MKTX");
  await expect(group).toContainText("2 legs");
  await expect(page.locator("tr.pos-leg")).toHaveCount(2);
  // Collapsible, and the plain rows are one switch away.
  await group.getByRole("button", { name: /Bull call spread/ }).click();
  await expect(page.locator("tr.pos-leg")).toHaveCount(0);
  await page.getByLabel("Group option strategies").uncheck();
  await expect(page.locator("tr.pos-group")).toHaveCount(0);
  await expect(page.locator(".bottom tbody tr").filter({ hasText: "Call" })).toHaveCount(2);
  await page.getByLabel("Group option strategies").check();

  // The multi-leg order is one row in Orders that opens to show its legs.
  await bottomTab(page, /^Orders/).click();
  await page.getByLabel("Show finished orders").check();
  const ml = page.locator("tr.ml-row");
  await expect(ml).toContainText("Bull call spread · MKTX");
  await expect(ml).toContainText("Multi-leg");
  await expect(ml).toContainText("debit");
  await ml.getByRole("button", { name: /Bull call spread/ }).click();
  await expect(page.locator("tr.ml-leg")).toHaveCount(2);
  await expect(page.locator("tr.ml-leg").first()).toContainText("Buy");

  // Close it: the builder opens with the opposite legs.
  await bottomTab(page, /^Positions/).click();
  await page.locator("tr.pos-group").getByRole("button", { name: "Close strategy…" }).click();
  const c = builder(page);
  await expect(c.getByRole("heading", { level: 3 })).toHaveText("Close bull call spread · MKTX");
  await expect(c.getByLabel("Quantity (units)")).toHaveValue("2");
  await expect(c.locator(".sb-leg").nth(0).getByLabel("Side")).toHaveValue("sell");
  await expect(c.locator(".sb-leg").nth(1).getByLabel("Side")).toHaveValue("buy");
  await expect(c.locator(".sb-net b").first()).toContainText("credit");
  await c.getByLabel("Order type").selectOption("market");
  await c.getByRole("button", { name: "Review order" }).click();
  await expect(fact(page, "Buying power after")).toHaveText("Unchanged (closing order)");
  await c.getByRole("button", { name: "Place paper order" }).click();
  await expect(page.getByText(/^Filled: Bear call spread, 2 × MKTX/)).toBeVisible();
  await expect(page.getByText("No open positions.")).toBeVisible();
});

test("iron condor in a margin account", async ({ page, open }) => {
  await open();
  const d = await openBuilder(page);
  await d.getByLabel("Strategy").selectOption("iron_condor");
  await expect(d.locator(".sb-leg")).toHaveCount(4);
  await expect(d.locator(".sb-name")).toHaveText("Iron condor");
  await expect(d.getByRole("button", { name: "Credit (you receive)" })).toHaveAttribute("aria-pressed", "true");
  await d.getByLabel("Order type").selectOption("market");
  const [res] = await Promise.all([page.waitForResponse(isPreview), d.getByRole("button", { name: "Review order" }).click()]);
  const pre = await res.json();
  expect(pre.ok).toBe(true);
  expect(pre.price_effect).toBe("credit");
  const credit = r2(pre.legs.reduce((a: number, l: any) => a + (l.side === "sell" ? l.bid : -l.ask), 0));
  await expect(fact(page, "Net price")).toHaveText(`${num(credit)} credit`);
  await expect(fact(page, "Total credit")).toHaveText(usd(credit * 100));
  await expect(fact(page, "Maximum profit")).toHaveText(usd(credit * 100));
  const strikes = pre.legs.map((l: any) => strikeOf(l.symbol));
  const width = strikes[1] - strikes[0];
  await expect(fact(page, "Maximum loss")).toHaveText(usd(r2((width - credit) * 100)));
  const bes = [r2(strikes[1] - credit), r2(strikes[2] + credit)];
  await expect(fact(page, "Break-even")).toHaveText(`${num(bes[0])} and ${num(bes[1])}`);
  const markers = d.getByRole("img", { name: /^Profit and loss/ }).locator("[data-breakeven]");
  await expect(markers).toHaveCount(2);
  expect(await markers.evaluateAll((els) => els.map((e) => e.getAttribute("data-breakeven")))).toEqual(bes.map((b) => b.toFixed(2)));
  await d.getByRole("button", { name: "Place paper order" }).click();
  await expect(page.getByText(/^Filled: Iron condor, 1 × MKTX at net [\d.]+ credit$/)).toBeVisible();
  await bottomTab(page, /^Positions/).click();
  await expect(page.locator("tr.pos-group")).toContainText("Iron condor · MKTX");
  await expect(page.locator("tr.pos-leg")).toHaveCount(4);
});

test("a credit spread in a cash account is refused with the server's reason", async ({ page, open, request }) => {
  const cash = await createAccount(request, "cash", "100000");
  await open(cash);
  const d = await openBuilder(page);
  await d.getByLabel("Strategy").selectOption("bear_call");
  await expect(d.locator(".sb-name")).toHaveText("Bear call spread");
  const [res] = await Promise.all([page.waitForResponse(isPreview), d.getByRole("button", { name: "Review order" }).click()]);
  const pre = await res.json();
  expect(pre.ok).toBe(false);
  expect(pre.reason).toContain("margin account");
  await expect(d.getByRole("alert")).toHaveText(`This order would be rejected: ${pre.reason}.`);
  await expect(d.getByRole("button", { name: "Place paper order" })).toHaveCount(0);
});

test("legs can be picked from the chain in add-to-strategy mode", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Options" }).click();
  await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
  await page.getByRole("button", { name: "Add to strategy" }).click();
  await expect(page.getByRole("button", { name: "Add to strategy" })).toHaveAttribute("aria-pressed", "true");
  const n = await page.locator(".chain-table tbody tr").count();
  const row = Math.floor(n / 2);
  const buy = page.locator(".chain-table tbody tr").nth(row).getByTitle("Add a buy leg").first();
  const sell = page.locator(".chain-table tbody tr").nth(row + 1).getByTitle("Add a sell leg").first();
  await buy.click();
  await sell.click();
  await expect(buy).toHaveAttribute("aria-pressed", "true");
  const tray = page.getByRole("region", { name: "Strategy legs picked" });
  await expect(tray).toContainText(/^Buy MKTX .* Call · Sell MKTX .* Call/);
  // Clicking a picked cell again removes it; the order ticket is not opened in this mode.
  await sell.click();
  await expect(tray.getByRole("button", { name: /Open in strategy builder \(1 leg\)/ })).toBeVisible();
  await sell.click();
  await tray.getByRole("button", { name: "Open in strategy builder (2 legs)" }).click();
  const d = builder(page);
  await expect(d.locator(".sb-leg")).toHaveCount(2);
  await expect(d.getByLabel("Strategy")).toHaveValue("custom");
  await expect(d.locator(".sb-name")).toHaveText("Bull call spread");
  await expect(d.locator(".sb-leg").nth(0).getByLabel("Side")).toHaveValue("buy");
  await expect(d.locator(".sb-leg").nth(1).getByLabel("Side")).toHaveValue("sell");
  await expect(d.locator("[data-leg-quote]").nth(1)).toContainText("Bid");
  await d.getByRole("button", { name: "Review order" }).click();
  await expect(fact(page, "Maximum loss")).toBeVisible();
  // Escape closes the builder.
  await page.keyboard.press("Escape");
  await expect(d).toHaveCount(0);
});

test("the builder is keyboard operable and does not resize the chart", async ({ page, open }) => {
  await open();
  const chart = page.getByRole("img", { name: "MKTX price chart" });
  const before = await chart.boundingBox();
  const d = await openBuilder(page);
  await expect(d.getByLabel("Strategy")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(d.locator(".sb-setup").getByLabel("Expiry")).toBeFocused();
  // Every input and select has a label.
  const unlabeled = await d.evaluate((el) => [...el.querySelectorAll("input, select")].filter((e) => !(e as HTMLInputElement).labels?.length && !e.getAttribute("aria-label")).length);
  expect(unlabeled).toBe(0);
  await d.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("tab", { name: "Chart" }).click();
  await expect.poll(async () => (await chart.boundingBox())?.height).toBe(before?.height);
});
