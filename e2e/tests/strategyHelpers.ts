import { expect } from "./fixtures";
import type { Locator, Page, Response } from "@playwright/test";

// Shared by the strategy builder tests on desktop (strategy.spec.ts) and phones (strategy.mobile.spec.ts).

export const usd = (n: number) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const num = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const strikeOf = (symbol: string) => Number(symbol.slice(-8)) / 1000;
export const r2 = (n: number) => Math.round(n * 100) / 100;

export const builder = (page: Page) => page.getByRole("dialog", { name: /^(Strategy builder|Close .*) · / });
export const fact = (page: Page, name: string) => builder(page).locator(".sb-preview dt", { hasText: new RegExp(`^${name}$`) }).locator("xpath=following-sibling::dd[1]");
export const isPreview = (r: Response) => r.url().includes("/multileg/preview") && r.request().method() === "POST";

export async function openBuilder(page: Page, tap = false) {
  const click = (l: Locator) => (tap ? l.tap() : l.click());
  await click(page.getByRole("tab", { name: "Options" }));
  await click(page.getByRole("button", { name: "Build a strategy…" }));
  const d = builder(page);
  await expect(d).toBeVisible();
  // The legs are built once the chain has loaded and every leg has a quote.
  await expect(d.locator("[data-leg-quote]").first()).toContainText("Bid");
  await expect(d.locator(".sb-net b").first()).not.toHaveText("–");
  return d;
}

// Checks the preview against figures computed here from the legs' quotes in the preview response.
export async function checkBullCall(page: Page, pre: any, qty: number) {
  const [long, short] = pre.legs;
  expect([long.side, short.side]).toEqual(["buy", "sell"]);
  const debit = r2(long.ask - short.bid);
  const width = strikeOf(short.symbol) - strikeOf(long.symbol);
  expect(width).toBeGreaterThan(0);
  await expect(fact(page, "Net price")).toHaveText(`${num(debit)} debit`);
  await expect(fact(page, "Total cost")).toHaveText(usd(debit * 100 * qty));
  await expect(fact(page, "Fees")).toHaveText(usd(0.65 * 2 * qty));
  await expect(fact(page, "Maximum loss")).toHaveText(usd(debit * 100 * qty));
  await expect(fact(page, "Maximum profit")).toHaveText(usd(r2((width - debit) * 100 * qty)));
  const be = r2(strikeOf(long.symbol) + debit);
  await expect(fact(page, "Break-even")).toHaveText(num(be));
  await expect(fact(page, "Margin or cash held")).toContainText("$0.00 →");
  // Payoff diagram: one break-even marker at the same price, and the current price marked.
  const chart = builder(page).getByRole("img", { name: /^Profit and loss at expiry/ });
  await expect(chart.locator("[data-breakeven]")).toHaveCount(1);
  await expect(chart.locator("[data-breakeven]")).toHaveAttribute("data-breakeven", be.toFixed(2));
  await expect(chart.locator("[data-breakeven] text")).toHaveText(`Break-even ${num(be)}`);
  await expect(chart.locator("[data-spot]")).toHaveCount(1);
  // The line crosses zero at the marker: the curve's points are [price, profit] pairs.
  const pts = (await chart.locator(".pf-line").getAttribute("data-points"))!.split(" ").map((p) => p.split(":").map(Number));
  const at = pts.find(([x]) => Math.abs(x - be) < 0.006);
  expect(at, "the curve has a point at the break-even").toBeTruthy();
  expect(Math.abs(at![1])).toBeLessThan(1 * qty);
  return be;
}


export const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

// Every visible control inside `root` smaller than 40 x 40 px (checkboxes sit inside a 40 px tall label).
export async function smallTargets(root: Locator, where: string): Promise<string[]> {
  const small = await root.evaluate((rootEl) => {
    const out: string[] = [];
    for (const el of rootEl.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]):not([type=radio]), [role=tab], a[href]")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || getComputedStyle(el).pointerEvents === "none") continue;
      if (r.width < 40 || r.height < 40) out.push(`${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    for (const el of rootEl.querySelectorAll<HTMLElement>("input[type=checkbox], input[type=radio]")) {
      const label = el.closest("label");
      const r = (label || el).getBoundingClientRect();
      if (r.width && r.height && r.height < 40) out.push(`checkbox "${(label?.textContent || "").trim().slice(0, 24)}" ${Math.round(r.height)} px tall`);
    }
    return out;
  });
  return small.map((s) => `${where}: ${s}`);
}

// Font sizes of the visible inputs and selects inside `root` (iOS zooms into anything below 16 px).
export const inputFontSizes = (root: Locator) => root.evaluate((el) => [...el.querySelectorAll<HTMLElement>("input:not([type=checkbox]), select")]
  .filter((e) => e.getBoundingClientRect().height > 0).map((e) => parseFloat(getComputedStyle(e).fontSize)));
