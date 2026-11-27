import { test, expect, ticket } from "./fixtures";
import type { Locator, Page } from "@playwright/test";

// Chart tools in the phone shell (run by the iphone-13 and pixel-7 projects): draw mode lets a tap-drag draw
// instead of panning, the toolbars wrap without horizontal scrolling, controls are 40 px, the legend stays off the
// Buy and Sell buttons, and trade at price (long-press) opens the Trade view with the ticket filled in.

// Leaving the page flushes any pending save (pagehide), so leave it and only then reset, before the next test.
test.beforeEach(async ({ request }) => { await request.delete("/api/chartstate"); });
test.afterEach(async ({ page, request }) => {
  await page.goto("/api/status").catch(() => {}); // a same-origin page without the app
  await page.waitForTimeout(500);
  await request.delete("/api/chartstate");
});

const chartView = (page: Page) => page.getByRole("region", { name: "Chart view" });

// A real touch gesture (Playwright has only tap). The CDP session is not detached afterwards: detaching it while
// Chrome is still finishing the gesture makes Chrome drop the click of the next tap.
async function touch(page: Page, from: { x: number; y: number }, to: { x: number; y: number } | null, holdMs = 0) {
  const cdp = await page.context().newCDPSession(page);
  const pt = (p: { x: number; y: number }) => [{ x: p.x, y: p.y, id: 1 }];
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pt(from) });
  if (holdMs) await page.waitForTimeout(holdMs);
  if (to)
    for (let i = 1; i <= 8; i++)
      await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: pt({ x: from.x + ((to.x - from.x) * i) / 8, y: from.y + ((to.y - from.y) * i) / 8 }) });
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

/** Wait until the element stops moving (the page settles as quotes, the clock and the chart arrive). */
async function stableBox(page: Page, loc: Locator) {
  let prev = await loc.boundingBox();
  for (let i = 0; i < 20; i++) {
    await page.waitForTimeout(250);
    const cur = await loc.boundingBox();
    if (prev && cur && Math.abs(prev.y - cur.y) < 0.5 && Math.abs(prev.height - cur.height) < 0.5) return cur;
    prev = cur;
  }
  return prev!;
}

async function ready(page: Page) {
  await expect(chartView(page)).toBeVisible();
  await expect(page.locator(".ct-stage")).toHaveAttribute("data-loaded", "1");
  await expect(page.getByTestId("chart-legend")).toBeVisible();
}

const scrolls = (page: Page) => page.evaluate(() => ({
  page: window.scrollY,
  view: document.querySelector<HTMLElement>(".m-view:not([hidden])")?.scrollTop ?? 0,
}));

test("draw a trend line by tap-drag in draw mode in the Chart view", async ({ page, open }) => {
  await open();
  await ready(page);
  await chartView(page).getByRole("button", { name: "Draw", exact: true }).tap();
  await page.getByRole("button", { name: "Trend line" }).tap();
  await expect(page.getByRole("button", { name: "Trend line" })).toHaveAttribute("aria-pressed", "true");
  const layer = page.getByTestId("draw-layer");
  await layer.scrollIntoViewIfNeeded();
  const box = await stableBox(page, layer);
  const before = await scrolls(page);
  await touch(page, { x: box.x + box.width * 0.2, y: box.y + box.height * 0.7 }, { x: box.x + box.width * 0.7, y: box.y + box.height * 0.3 });
  await expect(page.getByRole("button", { name: /^Drawings \(1\)/ })).toBeVisible();
  expect(await scrolls(page), "neither the page nor the view scrolled while drawing").toEqual(before);
  // Chrome can drop the click of a synthetic tap sent straight after a synthetic touch drag; pause as a person would.
  await page.waitForTimeout(600);
  await page.getByRole("button", { name: /^Drawings/ }).tap();
  await expect(page.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: /^Trend line from/ })).toBeVisible();
  // Saved: it is still there after a reload.
  await page.reload();
  await ready(page);
  await chartView(page).getByRole("button", { name: "Draw", exact: true }).tap();
  await expect(page.getByRole("button", { name: /^Drawings \(1\)/ })).toBeVisible();
});

test("chart toolbars fit the screen with 40 px controls and the legend stays off Buy and Sell", async ({ page, open }) => {
  await open();
  await ready(page);
  await chartView(page).getByRole("button", { name: "Draw", exact: true }).tap();
  const report = await page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>(".ct-bar button, .ct-bar select, .ct-bar input")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0) continue;
      if (r.height < 40 || r.width < 40) out.push(`${(el.getAttribute("aria-label") || el.textContent || "").trim()} ${Math.round(r.width)}x${Math.round(r.height)}`);
      if (r.right > document.documentElement.clientWidth + 0.5) out.push(`${el.textContent} overflows`);
    }
    return out;
  });
  expect(report).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  // The legend never sits over the Buy and Sell buttons.
  const legend = (await page.getByTestId("chart-legend").boundingBox())!;
  for (const name of [/^Buy /, /^Sell /]) {
    const b = (await chartView(page).getByRole("button", { name }).first().boundingBox())!;
    const overlap = !(legend.x + legend.width <= b.x || b.x + b.width <= legend.x || legend.y + legend.height <= b.y || b.y + b.height <= legend.y);
    expect(overlap, `legend overlaps ${name}`).toBe(false);
  }
  // Indicators panel at phone width: inside the screen.
  await chartView(page).getByRole("button", { name: /^Indicators/ }).tap();
  const panel = (await page.getByRole("dialog", { name: "Indicators" }).boundingBox())!;
  expect(panel.x).toBeGreaterThanOrEqual(0);
  expect(panel.x + panel.width).toBeLessThanOrEqual(page.viewportSize()!.width + 0.5);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
});

test("a long-press on the chart offers trade at price and opens the Trade view with the ticket filled in", async ({ page, open }) => {
  await open();
  await ready(page);
  const host = page.getByRole("figure", { name: "MKTX price chart" });
  await host.scrollIntoViewIfNeeded();
  const box = await stableBox(page, host);
  await touch(page, { x: box.x + box.width * 0.4, y: box.y + box.height * 0.3 }, null, 900);
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  const first = menu.getByRole("menuitem").first();
  const m = (await first.textContent())!.match(/^(Buy|Sell) (limit|stop) at ([\d,.]+)$/)!;
  expect(m).not.toBeNull();
  await page.waitForTimeout(400);
  await first.tap();
  await expect(page.getByRole("region", { name: "Trade view" })).toBeVisible();
  const t = ticket(page);
  await expect(t.getByLabel("Order type")).toHaveValue(m[2]);
  const input = t.getByLabel(m[2] === "limit" ? "Limit price" : "Stop price");
  await expect.poll(async () => Number(await input.inputValue())).toBe(Number(m[3].replace(/,/g, "")));
  await expect(t.getByRole("button", { name: "Place paper order" })).toHaveCount(0);
});
