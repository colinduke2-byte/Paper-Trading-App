import { test, expect } from "./fixtures";
import type { Locator, Page } from "@playwright/test";

// Chart tools on a phone (run by the iphone-13 and pixel-7 projects): draw mode lets a tap-drag draw instead of
// panning, the toolbars wrap without horizontal scrolling, and the chart controls are 40 px tall.
// Leaving the page flushes any pending save (pagehide), so leave it and only then reset, before the next test.
test.beforeEach(async ({ request }) => { await request.delete("/api/chartstate"); });
test.afterEach(async ({ page, request }) => {
  await page.goto("/api/status").catch(() => {}); // a same-origin page without the app
  await page.waitForTimeout(500);
  await request.delete("/api/chartstate");
});

// A real touch drag (Playwright has only tap). The CDP session is not detached afterwards: detaching it while
// Chrome is still finishing the gesture makes Chrome drop the click of the next tap.
async function touchDrag(page: Page, from: { x: number; y: number }, to: { x: number; y: number }) {
  const cdp = await page.context().newCDPSession(page);
  const pt = (p: { x: number; y: number }) => [{ x: p.x, y: p.y, id: 1 }];
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pt(from) });
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

test("draw a trend line by tap-drag in draw mode", async ({ page, open }) => {
  await open();
  await expect(page.locator(".ct-stage")).toHaveAttribute("data-loaded", "1");
  await expect(page.getByText(/^Market open/)).toBeVisible();
  await page.getByRole("button", { name: "Draw", exact: true }).tap();
  await page.getByRole("button", { name: "Trend line" }).tap();
  await expect(page.getByRole("button", { name: "Trend line" })).toHaveAttribute("aria-pressed", "true");
  const layer = page.getByTestId("draw-layer");
  await layer.scrollIntoViewIfNeeded();
  const box = await stableBox(page, layer);
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await touchDrag(page, { x: box.x + box.width * 0.2, y: box.y + box.height * 0.7 }, { x: box.x + box.width * 0.7, y: box.y + box.height * 0.3 });
  await expect(page.getByRole("button", { name: /^Drawings \(1\)/ })).toBeVisible();
  expect(await page.evaluate(() => window.scrollY), "the page did not scroll while drawing").toBe(scrollBefore);
  // Chrome can drop the click of a synthetic tap sent straight after a synthetic touch drag; pause as a person would.
  await page.waitForTimeout(600);
  await page.getByRole("button", { name: /^Drawings/ }).tap();
  await expect(page.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: /^Trend line from/ })).toBeVisible();
});

test("chart toolbars fit the screen with 40 px controls", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Draw", exact: true }).tap();
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
});
