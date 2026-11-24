import { test, expect, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";

// Desktop: several watchlists, columns and sorting, symbol search, the screener, the markets overview and compare.
// Each test makes its own uniquely named lists and screens and never edits the shared "Main" list.

const uniq = (p: string) => `${p} ${Date.now().toString(36).slice(-5)}${Math.floor(Math.random() * 100)}`;
const watch = (page: Page) => page.getByRole("region", { name: "Watchlist" });

async function newList(page: Page, name: string) {
  await watch(page).getByRole("button", { name: "Lists" }).click();
  await watch(page).getByRole("button", { name: "New list" }).click();
  await watch(page).getByLabel("New watchlist name").fill(name);
  await watch(page).getByRole("button", { name: "Create" }).click();
  await expect(watch(page).getByLabel("Choose watchlist")).toHaveValue(name);
}

async function addSymbol(page: Page, text: string, symbol: string) {
  const box = watch(page).getByLabel("Search symbols");
  await box.fill(text);
  await expect(watch(page).getByRole("listbox").getByRole("option").filter({ hasText: symbol }).first()).toBeVisible();
  await box.press("Enter");
  await expect(watch(page).getByRole("cell", { name: symbol, exact: true })).toBeVisible();
}

test("a second watchlist with its own columns and sort survives a reload", async ({ page, open, request }) => {
  await open();
  const name = uniq("E2E list");
  await newList(page, name);
  await addSymbol(page, "nvlx", "NVLX");
  await addSymbol(page, "orbit cl", "ORBT");
  // Keyboard navigation: the second result for "fund" is picked with the arrow key.
  const box = watch(page).getByLabel("Search symbols");
  await box.fill("fund");
  await expect(watch(page).getByRole("listbox").getByRole("option")).toHaveCount(2);
  const second = (await watch(page).getByRole("listbox").getByRole("option").nth(1).locator("b").textContent())!.trim();
  await box.press("ArrowDown");
  await expect(watch(page).getByRole("listbox").getByRole("option").nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(watch(page).getByRole("listbox").getByRole("option").nth(1)).toContainText(/ETF/);
  await box.press("Enter");
  await expect(watch(page).getByRole("cell", { name: second, exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: second, level: 2 })).toBeVisible();

  await watch(page).getByRole("button", { name: "Columns" }).click();
  const cols = watch(page).getByRole("group", { name: "Columns" });
  await cols.getByLabel("Volume").check();
  await cols.getByLabel("52-week range").check();
  await cols.getByLabel("Today").check();
  await cols.getByRole("button", { name: "Compact" }).click();
  await expect(watch(page).locator("thead")).toContainText("Volume");
  await expect(watch(page).getByRole("img", { name: /NVLX 52-week range/ })).toBeVisible();
  await expect(watch(page).getByRole("img", { name: "NVLX intraday line" })).toBeVisible();

  // Sort by change %, largest first; rows follow the values shown.
  await watch(page).getByRole("button", { name: /^Chg %/ }).click();
  await expect(watch(page).locator("th", { hasText: "Chg %" })).toHaveAttribute("aria-sort", "descending");
  const values = await watch(page).locator("tbody tr td:nth-child(4)").allTextContents();
  const nums = values.map((v) => parseFloat(v.replace("%", "").replace("+", "")));
  expect(nums).toEqual([...nums].sort((a, b) => b - a));

  // Saved on the server: a reload (new page, same server) shows the same list, columns, density and sort.
  const j = await (await request.get("/api/watchlist-sets")).json();
  const saved = j.lists.find((l: any) => l.name === name);
  expect(saved.symbols).toEqual(["NVLX", "ORBT", second]);
  expect(saved.layout).toEqual({ columns: ["last", "change", "change_pct", "volume", "range_52w", "sparkline"], density: "compact", sort: { key: "change_pct", desc: true } });
  await page.reload();
  await expect(watch(page).getByLabel("Choose watchlist")).toHaveValue(name);
  await expect(watch(page).locator("table.wl-compact thead")).toContainText("52-wk");

  // Reorder by hand after clearing the sort, then rename, duplicate and delete.
  await watch(page).getByRole("button", { name: "Reorder" }).click();
  await watch(page).getByRole("button", { name: "Clear the sort" }).click();
  await watch(page).getByRole("button", { name: "Move ORBT up" }).click();
  await expect.poll(async () => (await (await request.get("/api/watchlist-sets")).json()).lists.find((l: any) => l.name === name)?.symbols)
    .toEqual(["ORBT", "NVLX", second]);
  await watch(page).getByRole("button", { name: "Done" }).click();
  await watch(page).getByRole("button", { name: "Lists" }).click();
  await watch(page).getByRole("button", { name: "Rename" }).click();
  await watch(page).getByLabel("New name").fill(`${name} R`);
  await watch(page).getByRole("button", { name: "Rename" }).last().click();
  await expect(watch(page).getByLabel("Choose watchlist")).toHaveValue(`${name} R`);
  await watch(page).getByRole("button", { name: "Lists" }).click();
  await watch(page).getByRole("button", { name: "Delete" }).click();
  await watch(page).getByRole("button", { name: "Delete list" }).click();
  await expect(page.getByText(`Watchlist "${name} R" deleted`)).toBeVisible();
  await expect(watch(page).getByLabel("Choose watchlist").locator("option", { hasText: name })).toHaveCount(0);
});

test("a saved screen runs and its results go into a watchlist", async ({ page, open, request }) => {
  const list = uniq("Screened");
  await request.post("/api/watchlist-sets", { data: { name: list } });
  await open();
  await bottomTab(page, "Screener").click();
  const scr = page.locator(".screener");
  await expect(scr.getByRole("note")).toContainText("All 26 simulated symbols");
  await scr.getByLabel("Price minimum").fill("100");
  await scr.getByLabel("Add filter").selectOption({ label: "Asset type" });
  await expect(scr.locator(".sc-summary")).toContainText(/\d+ match/);
  const screen = uniq("E2E screen");
  await scr.getByLabel("Screen name").fill(screen);
  await scr.getByRole("button", { name: "Save screen" }).click();
  await expect(page.getByText(`Screen "${screen}" saved`)).toBeVisible();
  // Switch to a built-in screen, then back to the saved one: its filters come back and it runs.
  await scr.getByLabel("Saved screen").selectOption({ label: "Gapping up" });
  await expect(scr.getByLabel("Gap percent")).toBeVisible();
  await scr.getByLabel("Saved screen").selectOption({ label: screen });
  await expect(scr.getByLabel("Price minimum")).toHaveValue("100");
  await expect(scr.getByLabel("Asset type")).toHaveValue("stock");
  const rows = scr.locator(".sc-table tbody tr");
  await expect(rows.first()).toBeVisible();
  const lasts = (await scr.locator(".sc-table tbody tr td:nth-child(3)").allTextContents()).map((t) => parseFloat(t.replace(/,/g, "")));
  expect(lasts.length).toBeGreaterThan(0);
  for (const v of lasts) expect(v).toBeGreaterThanOrEqual(100);
  // Sort by a column header.
  await scr.getByRole("button", { name: /^Last/ }).click();
  await expect(scr.locator("th", { hasText: "Last" })).toHaveAttribute("aria-sort", "descending");
  await expect.poll(async () => {
    const v = (await scr.locator(".sc-table tbody tr td:nth-child(3)").allTextContents()).map((t) => parseFloat(t.replace(/,/g, "")));
    return v.join() === [...v].sort((a, b) => b - a).join();
  }).toBe(true);
  const n = await rows.count();
  await scr.getByLabel("Watchlist to add to").selectOption(list);
  await scr.getByRole("button", { name: "Add all to watchlist" }).click();
  await expect(page.getByText(`Added ${n} to ${list}`)).toBeVisible();
  const j = await (await request.get("/api/watchlist-sets")).json();
  expect(j.lists.find((l: any) => l.name === list).symbols).toHaveLength(n);
  await request.delete(`/api/watchlist-sets/${encodeURIComponent(list)}`);
});

test("the markets view shows movers, breadth and a heatmap that opens a symbol", async ({ page, open }) => {
  await open();
  await page.getByRole("tab", { name: "Markets" }).click();
  await expect(page.getByRole("group", { name: "Indexes and funds" }).getByRole("button")).toHaveCount(2);
  await expect(page.getByRole("region", { name: "Market breadth" })).toContainText("Rising");
  await expect(page.getByRole("region", { name: "Top gainers" }).locator("tbody tr")).toHaveCount(10);
  const tiles = page.getByRole("group", { name: "Sector heatmap" }).locator(".hm-tile");
  await expect(tiles).toHaveCount(24);
  const label = (await tiles.first().getAttribute("aria-label"))!;
  const sym = label.split(" ")[0];
  await tiles.first().click();
  await expect(page.getByRole("heading", { name: sym, level: 2 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Chart" })).toHaveAttribute("aria-selected", "true");
  // The ranked list and the movers also open symbols.
  await page.getByRole("tab", { name: "Markets" }).click();
  await page.getByRole("button", { name: "Ranked list" }).click();
  await expect(page.getByRole("list", { name: "Sectors ranked by change" }).locator("li")).toHaveCount(8);
  const loser = (await page.getByRole("region", { name: "Top losers" }).locator("tbody tr").first().locator("button").textContent())!;
  await page.getByRole("region", { name: "Top losers" }).getByRole("button", { name: loser, exact: true }).click();
  await expect(page.getByRole("heading", { name: loser, level: 2 })).toBeVisible();
});

test("compare shows performance for several symbols", async ({ page, open }) => {
  await open(undefined, "NVLX");
  await page.getByRole("tab", { name: "Compare" }).click();
  await page.evaluate(() => localStorage.removeItem("pd.compare"));
  const add = page.getByLabel("Add a symbol to compare");
  for (const s of ["ORBT", "MKTX"]) {
    await add.fill(s.toLowerCase());
    await expect(page.locator(".compare").getByRole("option").first()).toContainText(s);
    await add.press("Enter");
  }
  const table = page.locator(".cmp-table");
  await expect(table.locator("thead th")).toHaveCount(4);
  for (const p of ["1D", "1W", "1M", "3M", "1Y"]) await expect(table.getByRole("rowheader", { name: `Change ${p}` })).toBeVisible();
  await expect(table.getByRole("row", { name: /Change 1Y/ }).locator("td")).toHaveCount(3);
  await page.getByRole("button", { name: "Stop comparing ORBT" }).click();
  await expect(table.locator("thead th")).toHaveCount(3);
});
