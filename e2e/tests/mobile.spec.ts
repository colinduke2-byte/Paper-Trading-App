import { test, expect, createAccount, ticket, bottomTab } from "./fixtures";

// Phone profiles (see playwright.config.ts). Tests that fail on today's layout are marked test.fixme with
// the reason; the mobile UI stream is expected to fix the layout and remove the marks.

const BOTTOM = [/^Positions/, /^Orders/, "Fills", "Activity", "Performance", "Alerts", "Screener"];
const overflow = (page: any) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

test("no horizontal page scroll on any view", async ({ page, open }) => {
  await open();
  expect(await overflow(page), "on load").toBeLessThanOrEqual(0);
  for (const view of ["Options", "Company", "Chart"]) {
    await page.getByRole("tab", { name: view }).tap();
    expect(await overflow(page), `view ${view}`).toBeLessThanOrEqual(0);
  }
  for (const tab of BOTTOM) {
    await bottomTab(page, tab).dispatchEvent("click");
    expect(await overflow(page), `tab ${tab}`).toBeLessThanOrEqual(0);
  }
});

test.fixme("every bottom tab can be tapped (chart overlaps the tab bar)", async ({ page, open }) => {
  // FIXME: at phone width .chart (min-height 220px) overflows .main-view and covers the bottom panel's tabs, so taps are intercepted.
  await open();
  for (const tab of BOTTOM) {
    await bottomTab(page, tab).tap({ timeout: 3000 });
    await expect(bottomTab(page, tab)).toHaveAttribute("aria-selected", "true");
  }
});

test.fixme("interactive controls are at least 40 px tall and wide", async ({ page, open }) => {
  // FIXME: buttons, tabs and selects are sized for a mouse (about 24-30 px tall).
  await open();
  const small = await page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]), [role=tab]")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.width < 40 || r.height < 40) out.push(`${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    return out;
  });
  expect(small, "controls smaller than 40 px").toEqual([]);
});

test("order ticket inputs use a numeric keyboard", async ({ page, open }) => {
  await open();
  const t = ticket(page);
  await expect(t.getByLabel("Shares")).toHaveAttribute("inputmode", "decimal");
  await t.getByLabel("Order type").selectOption("limit");
  await expect(t.getByLabel("Limit price")).toHaveAttribute("inputmode", "decimal");
});

test.fixme("a market order can be placed by tapping", async ({ page, open }) => {
  // FIXME: the ticket sits below the chart and bottom panel on a phone, and the overlapping chart intercepts taps on the tabs.
  await open();
  const t = ticket(page);
  await t.getByLabel("Shares").fill("1");
  await t.getByRole("button", { name: /^Review/ }).tap({ timeout: 3000 });
  await t.getByRole("button", { name: "Place paper order" }).tap({ timeout: 3000 });
  await bottomTab(page, /^Positions/).tap({ timeout: 3000 });
  await expect(page.locator(".bottom tbody tr").filter({ hasText: "MKTX" })).toBeVisible();
});

test("tables scroll inside their own container", async ({ page, request, open }) => {
  // Place nothing: the watchlist, depth and chain tables are always populated.
  await open();
  await page.getByRole("tab", { name: "Options" }).tap();
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
