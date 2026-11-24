import { test, expect, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";

// Phone profiles: watchlists, columns and sort, a saved screen and the markets view, by tapping.

const uniq = (p: string) => `${p} ${Date.now().toString(36).slice(-5)}${Math.floor(Math.random() * 100)}`;
const nav = (page: Page, view: string) => page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: view, exact: true });
const watch = (page: Page) => page.getByRole("region", { name: "Watchlist", exact: true });
const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function smallTargets(page: Page, where: string): Promise<string[]> {
  const small = await page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]):not([type=radio]), [role=tab], a[href]")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || getComputedStyle(el).pointerEvents === "none") continue;
      if (r.width < 40 || r.height < 40) out.push(`${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    for (const el of document.querySelectorAll<HTMLElement>("input[type=checkbox]")) {
      const label = el.closest("label");
      const r = (label || el).getBoundingClientRect();
      if (r.width && r.height && r.height < 40) out.push(`checkbox "${(label?.textContent || "").trim().slice(0, 24)}" ${Math.round(r.height)} px tall`);
    }
    const inputs = [...document.querySelectorAll<HTMLElement>("input:not([type=checkbox]), select")]
      .filter((e) => e.getBoundingClientRect().height > 0 && parseFloat(getComputedStyle(e).fontSize) < 16);
    for (const e of inputs) out.push(`input "${e.getAttribute("aria-label")}" font below 16 px`);
    return out;
  });
  return small.map((s) => `${where}: ${s}`);
}

test("watchlists, a saved screen and the markets view work by tapping", async ({ page, open, request }) => {
  const screen = uniq("Phone screen");
  await request.post("/api/screens", { data: { name: screen, filters: [{ field: "last", min: 50 }], sort: "last", desc: true } });
  await open();
  const found: string[] = [];

  await nav(page, "Watchlist").tap();
  const name = uniq("Phone");
  await watch(page).getByRole("button", { name: "Lists" }).tap();
  found.push(...await smallTargets(page, "list actions"));
  await watch(page).getByRole("button", { name: "New list" }).tap();
  await watch(page).getByLabel("New watchlist name").fill(name);
  found.push(...await smallTargets(page, "new list form"));
  await watch(page).getByRole("button", { name: "Create" }).tap();
  await expect(watch(page).getByLabel("Choose watchlist")).toHaveValue(name);
  for (const s of ["NVLX", "AUTO", "TECX"]) {
    await watch(page).getByLabel("Search symbols").fill(s);
    await watch(page).getByRole("listbox").getByRole("option").filter({ hasText: s }).first().locator("button").tap();
    await expect(page.getByRole("heading", { name: s, level: 2 })).toBeVisible(); // picking a symbol opens its chart
    await nav(page, "Watchlist").tap();
    await expect(watch(page).getByRole("cell", { name: s, exact: true })).toBeVisible();
  }
  await watch(page).getByRole("button", { name: "Columns" }).tap();
  found.push(...await smallTargets(page, "column chooser"));
  await watch(page).getByRole("group", { name: "Columns" }).getByText("Volume", { exact: true }).tap();
  await watch(page).getByRole("group", { name: "Columns" }).getByText("Today", { exact: true }).tap();
  await watch(page).getByRole("button", { name: "Columns" }).tap();
  await expect(watch(page).locator("thead")).toContainText("Volume");
  await watch(page).getByRole("button", { name: /^Chg %/ }).tap();
  await expect(watch(page).locator("th", { hasText: "Chg %" })).toHaveAttribute("aria-sort", "descending");
  await watch(page).getByRole("button", { name: "Reorder" }).tap();
  found.push(...await smallTargets(page, "watchlist reorder"));
  expect(await overflow(page), "watchlist: no horizontal page scroll").toBeLessThanOrEqual(0);
  await watch(page).getByRole("button", { name: "Done" }).tap();

  // A saved screen from the Account view's Screener tab.
  await nav(page, "Account").tap();
  await bottomTab(page, "Screener").tap();
  const scr = page.locator(".screener");
  await scr.getByLabel("Saved screen").selectOption({ label: screen });
  await expect(scr.getByLabel("Price minimum")).toHaveValue("50");
  await expect(scr.locator(".sc-table tbody tr").first()).toBeVisible();
  found.push(...await smallTargets(page, "screener"));
  expect(await overflow(page), "screener: no horizontal page scroll").toBeLessThanOrEqual(0);

  // Markets: the ranked sector list is the phone default; tapping a chip opens that symbol.
  await nav(page, "Chart").tap();
  await page.getByRole("tab", { name: "Markets" }).tap();
  const list = page.getByRole("list", { name: "Sectors ranked by change" });
  await expect(list.locator("li")).toHaveCount(8);
  found.push(...await smallTargets(page, "markets"));
  expect(await overflow(page), "markets: no horizontal page scroll").toBeLessThanOrEqual(0);
  // The heatmap also fits the phone width.
  await page.getByRole("button", { name: "Heatmap" }).tap();
  const map = await page.getByRole("group", { name: "Sector heatmap" }).boundingBox();
  expect(map!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(await overflow(page), "heatmap: no horizontal page scroll").toBeLessThanOrEqual(0);
  await page.getByRole("button", { name: "Ranked list" }).tap();
  const chip = list.locator(".hm-chip").first();
  const sym = ((await chip.getAttribute("aria-label")) || "").split(" ")[0];
  await chip.tap();
  await expect(page.getByRole("heading", { name: sym, level: 2 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Chart" })).toHaveAttribute("aria-selected", "true");

  // Compare fits too.
  await page.getByRole("tab", { name: "Compare" }).tap();
  await page.getByLabel("Add a symbol to compare").fill("orbt");
  await page.locator(".compare").getByRole("option").first().locator("button").tap();
  found.push(...await smallTargets(page, "compare"));
  expect(await overflow(page), "compare: no horizontal page scroll").toBeLessThanOrEqual(0);

  expect(found, "controls smaller than 40 px or inputs below 16 px").toEqual([]);
  await request.delete(`/api/watchlist-sets/${encodeURIComponent(name)}`);
});
