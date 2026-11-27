import { test, expect, ticket, bottomTab } from "./fixtures";
import type { Page } from "@playwright/test";
import { threeLots } from "./lots-helpers";

// Tax lots in the phone layout (run by the phone projects): view lots, sell a chosen lot.

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function smallTargets(page: Page, selector: string): Promise<string[]> {
  return page.evaluate((sel) => {
    const out: string[] = [];
    for (const root of document.querySelectorAll<HTMLElement>(sel)) {
      for (const el of root.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]), summary")) {
        const r = el.getBoundingClientRect();
        if (r.width && r.height && (r.height < 40 || r.width < 40)) out.push(`${el.tagName} "${(el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 30)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
        if (el.tagName === "INPUT" && parseFloat(getComputedStyle(el).fontSize) < 16) out.push(`input font ${getComputedStyle(el).fontSize}`);
      }
      for (const el of root.querySelectorAll<HTMLElement>("input[type=checkbox]")) {
        const r = (el.closest("label") || el).getBoundingClientRect();
        if (r.height && r.height < 40) out.push(`checkbox label ${Math.round(r.height)} px tall`);
      }
    }
    return out;
  }, selector);
}

test("view lots and sell a chosen lot on a phone", async ({ page, open, account, request }) => {
  await threeLots(request, account.id);
  await open();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Account", exact: true }).tap();
  await bottomTab(page, "Lots").tap();
  const lots = page.getByRole("region", { name: /Lots of MKTX/ });
  await expect(lots.locator(".lot-row")).toHaveCount(3);
  expect(await overflow(page), "lots tab").toBeLessThanOrEqual(0);
  expect(await smallTargets(page, ".lots")).toEqual([]);
  await lots.getByText("L2", { exact: true }).tap();
  await expect(lots.getByLabel("Select lot L2")).toBeChecked();
  await lots.getByRole("button", { name: "Sell selected lots (1)" }).tap();

  await expect(page.getByRole("region", { name: "Trade view" })).toBeVisible();
  const t = ticket(page);
  await expect(t.getByLabel("Shares")).toHaveValue("5");
  await expect(t.getByLabel("Close lot L2")).toBeChecked();
  expect(await overflow(page), "ticket with lots").toBeLessThanOrEqual(0);
  expect(await smallTargets(page, ".lot-choice")).toEqual([]);
  await t.getByRole("button", { name: /^Review/ }).tap();
  await t.getByRole("button", { name: "Place paper order" }).tap();
  await expect(page.getByText(/Filled: sell 5 MKTX/)).toBeVisible();
  const after = await (await request.get(`/api/accounts/${account.id}/lots`)).json();
  expect(after.positions[0].lots.map((l: any) => l.id)).toEqual(["L1", "L3"]);
});
