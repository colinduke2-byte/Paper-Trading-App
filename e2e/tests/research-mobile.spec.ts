import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";

// Research sections in the phone shell: taps only, 40 px targets, no sideways page scroll.

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function smallTargets(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(".m-view:not([hidden]) :is(button, select, input, [role=tab], a[href])")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.width < 40 || r.height < 40) out.push(`${el.tagName.toLowerCase()} "${(el.textContent || "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    return out;
  });
}

test("research sections work on a phone", async ({ page, open }) => {
  await open(undefined, "NVLX");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Chart", exact: true }).tap();
  await page.getByRole("tab", { name: "Research" }).tap();
  const view = page.locator(".view");
  await expect(view.locator(".rs-source").first()).toContainText("Simulated");
  await expect(view.locator(".rs-news li").first()).toBeVisible();
  const found: string[] = [];
  found.push(...await smallTargets(page));
  expect(await overflow(page)).toBeLessThanOrEqual(0);

  await view.getByRole("tab", { name: "Statements" }).tap();
  const income = page.getByRole("region", { name: "Income statement" });
  await expect(income).toContainText("Revenue");
  const box = await income.boundingBox();
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);  // the table scrolls inside its box
  await page.getByRole("button", { name: "Annual" }).tap();
  await expect(income.locator("thead th").nth(1)).toHaveText(/^FY \d{4}$/);
  found.push(...await smallTargets(page));
  expect(await overflow(page)).toBeLessThanOrEqual(0);

  await view.getByRole("tab", { name: "Market and calendars" }).tap();
  await expect(page.locator(".rs-econ")).toContainText("Central bank rate decision");
  const sel = page.getByLabel("News category");
  expect(await sel.evaluate((e) => parseFloat(getComputedStyle(e).fontSize))).toBeGreaterThanOrEqual(16);
  found.push(...await smallTargets(page));
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  expect(found, "controls smaller than 40 px").toEqual([]);
});
