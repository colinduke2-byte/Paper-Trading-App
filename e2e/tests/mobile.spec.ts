import { test, expect, createAccount, lastPrice, ticket, bottomTab, confirmDialog } from "./fixtures";
import type { Page } from "@playwright/test";

// Phone profiles (see playwright.config.ts). Every interaction here is a real tap, so a control that is
// covered by another element fails the test instead of being clicked through.

const BOTTOM: (string | RegExp)[] = [/^Positions/, /^Orders/, "Fills", "Activity", "Reports", "Alerts", "Screener"];
const VIEWS = ["Watchlist", "Chart", "Trade", "Account"] as const;

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const navButton = (page: Page, view: string) => page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: view, exact: true });

async function go(page: Page, view: (typeof VIEWS)[number]) {
  await navButton(page, view).tap();
  await expect(navButton(page, view)).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("region", { name: `${view} view` })).toBeVisible();
}

// Every visible control smaller than 40 x 40 px (checkboxes sit inside a 40 px tall label).
async function smallTargets(page: Page, where: string): Promise<string[]> {
  const small = await page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]):not([type=radio]), [role=tab], a[href]")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || getComputedStyle(el).pointerEvents === "none") continue;
      if (r.width < 40 || r.height < 40) out.push(`${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    for (const el of document.querySelectorAll<HTMLElement>("input[type=checkbox], input[type=radio]")) {
      const label = el.closest("label");
      const r = (label || el).getBoundingClientRect();
      if (r.width && r.height && r.height < 40) out.push(`checkbox "${(label?.textContent || "").trim().slice(0, 24)}" ${Math.round(r.height)} px tall`);
    }
    return out;
  });
  return small.map((s) => `${where}: ${s}`);
}

// The view region fits between the top bar and the tab bar, and nothing scrolls the page sideways.
async function fitsViewport(page: Page, label: string) {
  const v = page.viewportSize()!;
  const nav = await page.getByRole("navigation", { name: "Views" }).boundingBox();
  expect(nav, `${label}: tab bar visible`).not.toBeNull();
  expect(nav!.y + nav!.height, `${label}: tab bar inside the viewport`).toBeLessThanOrEqual(v.height + 1);
  const view = await page.locator(".m-view:not([hidden])").boundingBox();
  expect(view!.y + view!.height, `${label}: view inside the viewport`).toBeLessThanOrEqual(v.height + 1);
  expect(await page.evaluate(() => document.documentElement.scrollHeight - document.documentElement.clientHeight), `${label}: no page scroll`).toBeLessThanOrEqual(0);
  expect(await overflow(page), `${label}: no horizontal page scroll`).toBeLessThanOrEqual(0);
}

async function placeFromTicket(page: Page, shares?: string) {
  const t = ticket(page);
  if (shares) await t.getByLabel("Shares").fill(shares);
  await t.getByRole("button", { name: /^Review/ }).tap();
  await t.getByRole("button", { name: "Place paper order" }).tap();
}

test("no horizontal page scroll on any view", async ({ page, open }) => {
  await open();
  expect(await overflow(page), "on load").toBeLessThanOrEqual(0);
  for (const view of ["Options", "Company", "Research", "Chart"]) {
    await page.getByRole("tab", { name: view }).tap();
    expect(await overflow(page), `view ${view}`).toBeLessThanOrEqual(0);
  }
  for (const view of VIEWS) {
    await go(page, view);
    await fitsViewport(page, view);
  }
  for (const tab of BOTTOM) {
    await bottomTab(page, tab).tap();
    expect(await overflow(page), `tab ${tab}`).toBeLessThanOrEqual(0);
  }
});

test("navigate all four views", async ({ page, open }) => {
  await open();
  await expect(navButton(page, "Chart")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("img", { name: "MKTX price chart" }).locator("canvas").first()).toBeVisible();

  await go(page, "Watchlist");
  await page.getByRole("region", { name: "Watchlist" }).getByRole("cell", { name: "TECX", exact: true }).tap();
  // Picking a symbol opens its chart.
  await expect(navButton(page, "Chart")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "TECX", level: 2 })).toBeVisible();
  const chart = page.getByRole("img", { name: "TECX price chart" });
  await expect.poll(async () => (await chart.boundingBox())?.height ?? 0).toBeGreaterThan(150);

  // The Buy button on the chart opens the ticket for that symbol.
  await page.getByRole("button", { name: "Buy TECX" }).tap();
  await expect(navButton(page, "Trade")).toHaveAttribute("aria-current", "page");
  await expect(ticket(page).getByText("TECX").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Depth and trades" })).toBeVisible();

  await go(page, "Account");
  await expect(page.locator(".m-acct")).toContainText("Buying power");
  await expect(bottomTab(page, /^Positions/)).toBeVisible();
});

test("every bottom tab can be tapped", async ({ page, open }) => {
  await open();
  await go(page, "Account");
  for (const tab of BOTTOM) {
    await bottomTab(page, tab).tap({ timeout: 3000 });
    await expect(bottomTab(page, tab)).toHaveAttribute("aria-selected", "true");
  }
});

test("interactive controls are at least 40 px tall and wide on every view", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const found: string[] = [];
  for (const view of ["Chart", "Options", "Company"]) {
    await page.getByRole("tab", { name: view }).tap();
    if (view === "Options") {
      await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
      found.push(...await smallTargets(page, "Options, calls"));
      await page.getByRole("button", { name: "Both" }).tap();
    }
    found.push(...await smallTargets(page, view));
  }
  await go(page, "Watchlist");
  found.push(...await smallTargets(page, "Watchlist"));
  await page.getByLabel("Search symbols").fill("TE");
  await expect(page.locator(".results button").first()).toBeVisible();
  found.push(...await smallTargets(page, "Watchlist search"));
  await page.getByLabel("Search symbols").fill("");

  await go(page, "Trade");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await t.getByLabel("Attach take profit and stop loss").check();
  found.push(...await smallTargets(page, "Trade, limit with bracket"));
  await t.getByLabel("Order type").selectOption("trailing_stop");
  found.push(...await smallTargets(page, "Trade, trailing stop"));
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await t.getByLabel("Shares").fill("1");
  await t.getByRole("button", { name: /^Review/ }).tap();
  await expect(t.getByRole("button", { name: "Place paper order" })).toBeVisible();
  found.push(...await smallTargets(page, "Trade, review"));
  await t.getByRole("button", { name: "Place paper order" }).tap();

  await go(page, "Account");
  for (const tab of BOTTOM) {
    await bottomTab(page, tab).tap();
    if (tab.toString().includes("Orders")) await expect(page.locator(".bottom tbody tr").filter({ hasText: "Working" })).toHaveCount(1);
    found.push(...await smallTargets(page, `Account ${tab}`));
  }

  await page.getByRole("button", { name: "Menu" }).tap();
  found.push(...await smallTargets(page, "Menu"));
  await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "New account" }).tap();
  await expect(page.getByRole("dialog")).toContainText("New paper account");
  found.push(...await smallTargets(page, "New account dialog"));
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).tap();

  expect(found, "controls smaller than 40 px").toEqual([]);
});

test("order ticket inputs use a numeric keyboard and do not zoom on focus", async ({ page, open }) => {
  await open();
  await go(page, "Trade");
  const t = ticket(page);
  await expect(t.getByLabel("Shares")).toHaveAttribute("inputmode", "decimal");
  await t.getByLabel("Order type").selectOption("limit");
  await expect(t.getByLabel("Limit price")).toHaveAttribute("inputmode", "decimal");
  const sizes = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("input:not([type=checkbox]), select")]
    .filter((e) => e.getBoundingClientRect().height > 0).map((e) => parseFloat(getComputedStyle(e).fontSize)));
  expect(sizes.length).toBeGreaterThan(2);
  for (const s of sizes) expect(s, "input font size (iOS zooms below 16 px)").toBeGreaterThanOrEqual(16);
});

test("a market order can be placed by tapping", async ({ page, open }) => {
  await open();
  await go(page, "Trade");
  await placeFromTicket(page, "1");
  await expect(page.getByText(/Filled: buy 1 MKTX/)).toBeVisible();
  await go(page, "Account");
  await bottomTab(page, /^Positions/).tap({ timeout: 3000 });
  const row = page.locator(".bottom tbody tr").filter({ hasText: "MKTX" });
  await expect(row).toBeVisible();
  await expect(row.getByRole("cell").nth(1)).toHaveText("1");
});

test("a position can be closed from the account view", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Buy MKTX" }).tap();
  await placeFromTicket(page, "4");
  await expect(page.getByText(/Filled: buy 4 MKTX/)).toBeVisible();
  await go(page, "Account");
  await page.locator(".bottom tbody tr").filter({ hasText: "MKTX" }).getByRole("button", { name: "Close…" }).tap();
  // Closing opens the ticket, prefilled to sell the whole position.
  await expect(navButton(page, "Trade")).toHaveAttribute("aria-current", "page");
  const t = ticket(page);
  await expect(t.getByLabel("Shares")).toHaveValue("4");
  await expect(t.getByRole("button", { name: /^Review: Sell/ })).toBeVisible();
  await placeFromTicket(page);
  await expect(page.getByText(/Filled: sell 4 MKTX/)).toBeVisible();
  await go(page, "Account");
  await expect(page.getByText("No open positions.")).toBeVisible();
});

test("a bracket order leaves both exit orders working", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await go(page, "Trade");
  const t = ticket(page);
  await t.getByLabel("Attach take profit and stop loss").tap();
  await expect(t.getByLabel("Attach take profit and stop loss")).toBeChecked();
  await t.getByLabel("Take profit", { exact: true }).fill((px * 1.5).toFixed(2));
  await t.getByLabel("Stop loss", { exact: true }).fill((px * 0.5).toFixed(2));
  await placeFromTicket(page, "5");
  await expect(page.getByText(/Filled: buy 5 MKTX/)).toBeVisible();
  await go(page, "Account");
  await bottomTab(page, /^Orders/).tap();
  const rows = page.locator(".bottom tbody tr");
  await expect(rows.filter({ hasText: "take profit" }).filter({ hasText: "Working" })).toHaveCount(1);
  await expect(rows.filter({ hasText: "stop loss" }).filter({ hasText: "Working" })).toHaveCount(1);
});

test("a working order can be canceled", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await go(page, "Trade");
  const t = ticket(page);
  await t.getByLabel("Order type").selectOption("limit");
  await t.getByLabel("Limit price").fill((px * 0.5).toFixed(2));
  await placeFromTicket(page, "3");
  await expect(page.getByText(/Order placed: buy 3 MKTX/)).toBeVisible();
  await go(page, "Account");
  await bottomTab(page, /^Orders/).tap();
  const row = page.locator(".bottom tbody tr").filter({ hasText: "Working" });
  await expect(row).toHaveCount(1);
  // The Cancel button sits in the last column; the table scrolls sideways inside its own container.
  await row.getByRole("button", { name: "Cancel" }).tap();
  await expect(page.getByText("Order canceled")).toBeVisible();
  await expect(page.getByText("No open orders.")).toBeVisible();
  await page.getByLabel("Show finished orders").tap();
  await expect(page.locator(".bottom tbody tr").filter({ hasText: "Canceled" })).toHaveCount(1);
});

test("option chain: buy one call", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Options" }).tap();
  await expect(page.getByText("model prices, not market data")).toBeVisible();
  // Calls are shown first, one side at a time.
  await expect(page.getByRole("button", { name: "Calls" })).toHaveAttribute("aria-pressed", "true");
  await page.getByTitle("Buy at the ask").first().tap();
  await expect(navButton(page, "Trade")).toHaveAttribute("aria-current", "page");
  const t = ticket(page);
  await expect(t.getByLabel("Contracts")).toHaveValue("1");
  await expect(t.getByText(/Call/).first()).toBeVisible();
  await placeFromTicket(page);
  await expect(page.getByText(/(Filled|Order placed): buy 1 MKTX/)).toBeVisible();
  await go(page, "Account");
  await bottomTab(page, /^Positions/).tap();
  await expect(page.locator(".bottom tbody tr").filter({ hasText: /Call/ })).toBeVisible();
});

test("option chain: puts and both sides", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Options" }).tap();
  await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
  const visibleHeads = () => page.locator(".chain-table thead tr").last().locator("th:visible").allTextContents();
  expect(await visibleHeads()).toEqual(["IV", "Delta", "Bid", "Ask", "Strike"]);
  await page.getByRole("button", { name: "Puts" }).tap();
  expect(await visibleHeads()).toEqual(["Strike", "Bid", "Ask", "Delta", "IV"]);
  await page.getByRole("button", { name: "Both" }).tap();
  expect((await visibleHeads()).length).toBe(13);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
});

test("switching account from the menu changes the figures", async ({ page, request, open }) => {
  const small = await createAccount(request, "cash", "1234");
  const big = await createAccount(request, "margin", "56789");
  await open(small);
  const top = page.locator(".m-stats");
  await expect(top).toContainText("$1,234.00");
  await page.getByRole("button", { name: "Menu" }).tap();
  const menu = page.getByRole("dialog", { name: "Menu" });
  await menu.getByRole("combobox", { name: "Account" }).selectOption(big.id);
  await expect(menu).toHaveCount(0); // picking an account closes the menu
  await expect(top).toContainText("$56,789.00");
  await go(page, "Account");
  await expect(page.locator(".m-acct")).toContainText(big.name);
  await expect(page.locator(".m-acct")).toContainText("Maintenance margin");
  await page.getByRole("button", { name: "Menu" }).tap();
  await page.getByRole("dialog", { name: "Menu" }).getByRole("combobox", { name: "Account" }).selectOption(small.id);
  await expect(top).toContainText("$1,234.00");
});

test("create an account from the menu", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Menu" }).tap();
  await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "New account" }).tap();
  const dialog = page.getByRole("dialog");
  const name = `Phone ${Date.now().toString(36)}`;
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Starting cash").fill("5000");
  await dialog.getByRole("button", { name: "Create account" }).tap();
  await expect(page.getByText(`"${name}" created and selected.`)).toBeVisible();
  await expect(page.locator(".m-stats")).toContainText("$5,000.00");
});

test("landscape orientation fits without overflow", async ({ page, open }) => {
  const v = page.viewportSize()!;
  await open();
  await page.setViewportSize({ width: v.height + 100, height: v.width - 40 }); // rotate (browser bars take some height)
  await expect(navButton(page, "Chart")).toBeVisible();
  for (const view of VIEWS) {
    await go(page, view);
    await fitsViewport(page, `landscape ${view}`);
    expect(await smallTargets(page, `landscape ${view}`)).toEqual([]);
  }
  await go(page, "Chart");
  await expect(page.getByRole("note")).toBeVisible();
  const chart = page.getByRole("img", { name: "MKTX price chart" });
  await expect.poll(async () => (await chart.boundingBox())?.height ?? 0).toBeGreaterThan(100);
  for (const view of ["Options", "Company"]) {
    await page.getByRole("tab", { name: view }).tap();
    await fitsViewport(page, `landscape ${view}`);
  }
  // And back to portrait.
  await page.setViewportSize(v);
  await go(page, "Trade");
  await fitsViewport(page, "portrait again");
});

test("tables scroll inside their own container", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Options" }).tap();
  await page.getByRole("button", { name: "Both" }).tap();
  await expect(page.getByTitle("Buy at the ask").first()).toBeVisible();
  const info = await page.evaluate(() => {
    const t = document.querySelector<HTMLElement>(".chain-table")!;
    let p: HTMLElement | null = t.parentElement;
    while (p && getComputedStyle(p).overflowX === "visible") p = p.parentElement;
    return { tableWidth: t.scrollWidth, scroller: p && p.tagName !== "HTML" ? { overflowX: getComputedStyle(p).overflowX, client: p.clientWidth } : null };
  });
  expect(info.scroller, "chain table needs a scrolling container").not.toBeNull();
  expect(["auto", "scroll"]).toContain(info.scroller!.overflowX);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
});

test.describe("replay", () => {
  // Replay mode is global on the server; never leave it running for the next test.
  test.afterEach(async ({ request }) => { await request.post("/api/replay/exit").catch(() => {}); });

  test("start a replay from the menu, play, pause, step, trade and exit", async ({ page, open, request }) => {
    await open();
    await page.getByRole("button", { name: "Menu" }).tap();
    await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Replay" }).tap();
    const picker = page.getByRole("dialog", { name: "Replay a trading session" });
    await expect(picker.getByRole("radio").first()).toBeChecked();
    expect(await smallTargets(page, "Replay picker")).toEqual([]);
    await picker.getByRole("button", { name: /^Start with/ }).tap();
    await expect(picker).toHaveCount(0);

    const banner = page.getByRole("note");
    await expect(banner).toContainText("Replay of");
    await expect(banner).toContainText("paper trading, no real money");
    const bar = page.getByRole("region", { name: "Replay controls" });
    await expect(bar).toBeVisible();
    // The banner is never cut off, and every view still fits with the controls showing.
    expect(await banner.evaluate((e) => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight + 1)).toBe(true);
    for (const view of VIEWS) {
      await go(page, view);
      await fitsViewport(page, `replay ${view}`);
      expect(await smallTargets(page, `replay ${view}`)).toEqual([]);
    }
    await go(page, "Chart");

    // Play, then pause.
    await bar.getByRole("button", { name: "Play", exact: true }).tap();
    await expect(bar.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
    await bar.getByRole("button", { name: "Pause", exact: true }).tap();
    await expect(bar.getByRole("button", { name: "Play", exact: true })).toBeVisible();

    // The step controls sit behind "More".
    await page.getByRole("button", { name: "More replay controls" }).tap();
    await expect(bar.getByRole("button", { name: "+1 min" })).toBeVisible();
    for (const view of VIEWS) {
      await go(page, view);
      await fitsViewport(page, `replay expanded ${view}`);
    }
    expect(await smallTargets(page, "replay expanded")).toEqual([]);
    await go(page, "Chart");
    const clock = bar.locator(".replay-clock");
    const before = await clock.textContent();
    const chart = page.getByRole("img", { name: /price chart$/ });
    await chart.evaluate((e: any) => { e.__beforeStep = true; });
    await bar.getByRole("button", { name: "+1 min" }).tap();
    await expect(clock).not.toHaveText(before!);
    // The chart reloads its history after a jump (it is a new element).
    await expect.poll(() => chart.evaluate((e: any) => !!e.__beforeStep)).toBe(false);
    await page.getByRole("button", { name: "Fewer replay controls" }).tap();

    // Trade in the sandbox account.
    const sandbox = await page.locator(".m-shell").getAttribute("data-account");
    await go(page, "Trade");
    await placeFromTicket(page, "2");
    await expect(page.getByText(/Filled: buy 2 /)).toBeVisible();
    await go(page, "Account");
    await expect(page.locator(".bottom tbody tr").first()).toContainText("2");

    // Exit back to the live accounts.
    await bar.getByRole("button", { name: "Exit replay" }).tap();
    await confirmDialog(page, "Exit replay", true);
    await expect(page.getByText("Replay closed. You are back on your paper accounts.")).toBeVisible();
    await expect(bar).toHaveCount(0);
    await expect(banner).toContainText("Paper trading · no real money");
    const live = (await (await request.get("/api/accounts")).json()).map((a: any) => a.id);
    await expect.poll(() => page.locator(".m-shell").getAttribute("data-account")).not.toBe(sandbox);
    expect(live).toContain(await page.locator(".m-shell").getAttribute("data-account"));
    await page.getByRole("button", { name: "Menu" }).tap();
    await expect(page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Replay" })).toBeVisible();
  });
});

test("Settings, Research and Reports work inside the phone shell", async ({ page, open }) => {
  await open();
  const found: string[] = [];
  // Settings is a menu entry; Sign out is not, because this server has no passcode (see auth.spec.ts).
  await page.getByRole("button", { name: "Menu" }).tap();
  const menu = page.getByRole("dialog", { name: "Menu" });
  await expect(menu.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  await menu.getByRole("button", { name: "Account rules" }).tap();
  const settings = page.getByRole("dialog");
  await expect(settings).toContainText("Account rules");
  found.push(...await smallTargets(page, "Settings dialog"));
  expect(await overflow(page), "Settings: no horizontal page scroll").toBeLessThanOrEqual(0);
  const box = await settings.boundingBox();
  if (box) expect(box.x + box.width, "Settings dialog fits the width").toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  await settings.getByRole("button", { name: /^(Cancel|Close)$/ }).first().tap();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await go(page, "Chart");
  await page.getByRole("tab", { name: "Research" }).tap();
  await expect(page.getByRole("tab", { name: "Research" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".view")).toContainText(/Research data is unavailable|News/);
  found.push(...await smallTargets(page, "Research"));
  await fitsViewport(page, "Research");

  await go(page, "Account");
  await bottomTab(page, "Reports").tap();
  for (const sub of ["Overview", "Gains", "Statements", "Journal", "Export"]) {
    await page.locator(".bottom").getByRole("button", { name: sub, exact: true }).tap();
    await expect(page.locator(".bottom").getByRole("button", { name: sub, exact: true })).toHaveAttribute("aria-pressed", "true");
    found.push(...await smallTargets(page, `Reports ${sub}`));
    await fitsViewport(page, `Reports ${sub}`);
  }
  expect(found, "controls smaller than 40 px").toEqual([]);
});

test("Help opens from the menu, fits a phone and has 40 px targets", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Menu" }).tap();
  await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Help", exact: true }).tap();
  const help = page.getByRole("dialog", { name: "Help" });
  await expect(help).toBeVisible();
  await expect(help).toContainText("Getting started");
  const found: string[] = [];
  // Open every section: the targets must be big enough and nothing may push the page sideways.
  const sections = help.locator("summary");
  const n = await sections.count();
  expect(n).toBeGreaterThanOrEqual(5);
  for (let i = 0; i < n; i++) {
    const s = sections.nth(i);
    const open = await s.evaluate((el) => (el.parentElement as HTMLDetailsElement).open);
    if (!open) await s.tap();
    const box = await s.boundingBox();
    if (box && box.height < 40) found.push(`summary ${i} is ${Math.round(box.height)} px tall`);
  }
  found.push(...await smallTargets(page, "Help dialog"));
  expect(await overflow(page), "Help: no horizontal page scroll").toBeLessThanOrEqual(0);
  const box = await help.boundingBox();
  expect(box!.x, "Help dialog starts inside the screen").toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width, "Help dialog fits the width").toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  expect(box!.y + box!.height, "Help dialog fits the height").toBeLessThanOrEqual(page.viewportSize()!.height + 1);
  expect(found, "targets under 40 px").toEqual([]);
  await help.getByRole("button", { name: "Close help" }).tap();
  await expect(help).toHaveCount(0);
});
