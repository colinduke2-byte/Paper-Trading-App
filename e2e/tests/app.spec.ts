import { test, expect, createAccount, lastPrice, ticket, bottomTab, review, place, confirmDialog } from "./fixtures";

test("page loads with the paper trading banner", async ({ page, open }) => {
  await open();
  await expect(page.getByRole("note")).toContainText("Paper trading · no real money");
  await expect(page.getByRole("note")).toContainText("simulated market data");
  await expect(page.getByRole("region", { name: "Watchlist" })).toBeVisible();
  await expect(page.getByText("Market open")).toBeVisible();
});

test("selecting a symbol shows its quote and renders the chart", async ({ page, open }) => {
  await open();
  await page.getByRole("region", { name: "Watchlist" }).getByRole("cell", { name: "TECX", exact: true }).click();
  await expect(page.getByRole("heading", { name: "TECX", level: 2 })).toBeVisible();
  const chart = page.getByRole("img", { name: "TECX price chart" });
  await expect(chart).toBeVisible();
  await expect(chart.locator("canvas").first()).toBeVisible();
  await expect.poll(async () => (await chart.boundingBox())?.height ?? 0).toBeGreaterThan(100);
  await expect(ticket(page).getByText("TECX").first()).toBeVisible();
});

test("a market order fills and the position appears", async ({ page, open }) => {
  await open();
  await review(page, "10");
  await expect(page.getByText("Estimated cost")).toBeVisible();
  await place(page);
  await expect(page.getByText(/Filled: buy 10 MKTX/)).toBeVisible();
  await bottomTab(page, /^Positions/).click();
  const row = page.locator(".bottom tbody tr").filter({ hasText: "MKTX" });
  await expect(row).toBeVisible();
  await expect(row.getByRole("cell").nth(1)).toHaveText("10");
});

test("a bracket order leaves both exit orders working", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const t = ticket(page);
  await t.getByLabel("Attach take profit and stop loss").check();
  await t.getByLabel("Take profit", { exact: true }).fill((px * 1.5).toFixed(2));
  await t.getByLabel("Stop loss", { exact: true }).fill((px * 0.5).toFixed(2));
  await review(page, "5");
  await place(page);
  await expect(page.getByText(/Filled: buy 5 MKTX/)).toBeVisible();
  await bottomTab(page, /^Orders/).click();
  const rows = page.locator(".bottom tbody tr");
  await expect(rows.filter({ hasText: "take profit" }).filter({ hasText: "Working" })).toHaveCount(1);
  await expect(rows.filter({ hasText: "stop loss" }).filter({ hasText: "Working" })).toHaveCount(1);
});

test("a working order can be canceled", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await review(page, "3");
  await place(page);
  await bottomTab(page, /^Orders/).click();
  const row = page.locator(".bottom tbody tr").filter({ hasText: "Working" });
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByText("Order canceled")).toBeVisible();
  await expect(page.getByText("No open orders.")).toBeVisible();
  await page.getByLabel("Show finished orders").check();
  await expect(page.locator(".bottom tbody tr").filter({ hasText: "Canceled" })).toHaveCount(1);
});

test("a rejected order shows the real reason", async ({ page, request, open }) => {
  const small = await createAccount(request, "cash", "1000");
  await open(small);
  await review(page, "100000");
  await expect(page.getByRole("alert")).toContainText("This order would be rejected: insufficient cash.");
  await expect(page.getByRole("button", { name: "Place paper order" })).toHaveCount(0);
});

test("option chain: buy one call", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Options" }).click();
  await expect(page.getByText("model prices, not market data")).toBeVisible();
  await page.getByTitle("Buy at the ask").first().click();
  const t = ticket(page);
  await expect(t.getByLabel("Contracts")).toHaveValue("1");
  await t.getByRole("button", { name: /^Review/ }).click();
  await place(page);
  await expect(page.getByText(/(Filled|Order placed): buy 1 MKTX/)).toBeVisible();
  await bottomTab(page, /^Positions/).click();
  await expect(page.locator(".bottom tbody tr").filter({ hasText: /Call/ })).toBeVisible();
});

test("switching account changes the figures", async ({ page, request, open }) => {
  const small = await createAccount(request, "cash", "1234");
  const big = await createAccount(request, "margin", "56789");
  await open(small);
  const top = page.locator(".top-stats");
  await expect(top).toContainText("$1,234.00");
  const select = page.getByRole("combobox", { name: "Account" });
  await select.selectOption(big.id);
  await expect(top).toContainText("$56,789.00");
  await expect(top).toContainText("Maintenance margin");
  await select.selectOption(small.id);
  await expect(top).toContainText("$1,234.00");
});

test("create an account, then reset it", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "New account" }).click();
  const dialog = page.getByRole("dialog");
  const name = `Created ${Date.now().toString(36)}`;
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Type").selectOption("cash");
  await dialog.getByLabel("Starting cash").fill("5000");
  await dialog.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText(`"${name}" created and selected.`)).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Account" }).locator("option:checked")).toContainText(name);
  await expect(page.locator(".top-stats")).toContainText("$5,000.00");

  await review(page, "2");
  await place(page);
  await expect(page.getByText(/Filled: buy 2 MKTX/)).toBeVisible();
  await expect(page.getByText("No open positions.")).toHaveCount(0);

  await page.getByRole("button", { name: "Reset account", exact: true }).click();
  await page.getByRole("dialog").getByLabel("Starting cash").fill("7000");
  await page.getByRole("dialog").getByRole("button", { name: "Erase and reset" }).click();
  await expect(page.getByText(/reset to \$7,000\.00/)).toBeVisible();
  await expect(page.getByText("No open positions.")).toBeVisible();
  await expect(page.locator(".top-stats")).toContainText("$7,000.00");
});

test("every bottom tab renders", async ({ page, open }) => {
  await open();
  const tabs: [string | RegExp, string | RegExp][] = [
    [/^Positions/, "No open positions."],
    [/^Orders/, "No open orders."],
    ["Fills", "No fills yet."],
    ["Activity", "Entry"],
    ["Reports", "Returns and risk"],
    ["Alerts", "No price alerts."],
    ["Screener", "Sector"],
  ];
  for (const [tab, expected] of tabs) {
    await bottomTab(page, tab).click();
    await expect(bottomTab(page, tab)).toHaveAttribute("aria-selected", "true");
    if (tab === "Screener") {
      await expect(page.getByLabel("Sector")).toBeVisible();
      await expect(page.locator(".bottom tbody tr").first()).toBeVisible();
    } else {
      await expect(page.locator(".bottom .scroll")).toContainText(expected);
    }
  }
  for (const view of ["Options", "Company", "Research", "Chart"]) {
    await page.getByRole("tab", { name: view }).click();
    await expect(page.getByRole("tab", { name: view })).toHaveAttribute("aria-selected", "true");
  }
  await page.getByRole("tab", { name: "Company" }).click();
  await expect(page.getByText("Every company in this build is fictional")).toBeVisible();
});

test("no horizontal overflow at 420 px width", async ({ page, open }) => {
  // Below 900 px the phone layout takes over: one view at a time with a tab bar at the bottom.
  await page.setViewportSize({ width: 420, height: 900 });
  await open();
  await expect(page.getByRole("note")).toBeVisible();
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const nav = (name: string) => page.getByRole("navigation", { name: "Views" }).getByRole("button", { name, exact: true });
  expect(await overflow(), "overflow on load").toBeLessThanOrEqual(0);
  await nav("Account").click();
  for (const tab of [/^Positions/, /^Orders/, "Fills", "Activity", "Reports", "Alerts", "Screener"]) {
    await bottomTab(page, tab).click();
    await expect(bottomTab(page, tab)).toHaveAttribute("aria-selected", "true");
    expect(await overflow(), `horizontal overflow on tab ${tab}`).toBeLessThanOrEqual(0);
  }
  await nav("Chart").click();
  for (const view of ["Options", "Company", "Research", "Chart"]) {
    await page.getByRole("tab", { name: view }).click();
    await expect(page.getByRole("tab", { name: view })).toHaveAttribute("aria-selected", "true");
    expect(await overflow(), `horizontal overflow on view ${view}`).toBeLessThanOrEqual(0);
  }
  for (const view of ["Watchlist", "Trade"]) {
    await nav(view).click();
    expect(await overflow(), `horizontal overflow on view ${view}`).toBeLessThanOrEqual(0);
  }
});

test("the Replay button starts a replay and Exit returns to live", async ({ page, open, request }) => {
  await open();
  try {
    await page.getByRole("button", { name: "Replay", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "Replay a trading session" });
    await picker.getByRole("button", { name: /^Start with/ }).click();
    await expect(page.getByRole("note")).toContainText("Replay of");
    const bar = page.getByRole("region", { name: "Replay controls" });
    await bar.getByRole("button", { name: "+1 min" }).click();
    await bar.getByRole("button", { name: "Exit replay" }).click();
    await confirmDialog(page, "Exit replay");
    await expect(bar).toHaveCount(0);
    await expect(page.getByRole("note")).toContainText("Paper trading · no real money");
    await expect(page.getByRole("button", { name: "Replay", exact: true })).toBeVisible();
  } finally {
    await request.post("/api/replay/exit").catch(() => {});
  }
});

test("the Help button opens the in-app help and Escape closes it", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Help", exact: true }).click();
  const help = page.getByRole("dialog", { name: "Help" });
  await expect(help).toBeVisible();
  await expect(help).toContainText("Getting started");
  await expect(help).toContainText("Paper trading · no real money");
  await help.getByText("Order types", { exact: true }).click();
  await expect(help).toContainText("Trailing stop");
  await expect(help).toContainText("Fill or kill");
  await page.keyboard.press("Escape");
  await expect(help).toHaveCount(0);
  await page.getByRole("button", { name: "Help", exact: true }).click();
  await help.getByRole("button", { name: "Close help" }).click();
  await expect(help).toHaveCount(0);
});
