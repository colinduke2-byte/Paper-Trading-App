import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";

// Data and backups on a phone (one profile: Pixel 7).
test.beforeEach(({}, info) => { test.skip(info.project.name !== "pixel-7", "one phone profile is enough here"); });

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function smallIn(page: Page, selector: string): Promise<string[]> {
  return page.evaluate((sel) => {
    const out: string[] = [];
    const root = document.querySelector(sel);
    if (!root) return ["dialog missing"];
    for (const el of root.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]):not([type=radio]):not([hidden]), a[href]")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.width < 40 || r.height < 40) out.push(`${(el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 24)} ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    for (const el of root.querySelectorAll<HTMLInputElement>("input:not([type=file])")) {
      if (parseFloat(getComputedStyle(el).fontSize) < 16) out.push(`input text ${getComputedStyle(el).fontSize}`);
    }
    const box = (root as HTMLElement).getBoundingClientRect();
    if (box.right > window.innerWidth + 1 || box.left < -1) out.push(`dialog ${Math.round(box.left)}..${Math.round(box.right)} of ${window.innerWidth}`);
    return out;
  }, selector);
}

test("data and backups from the phone menu: back up and restore", async ({ page, open, request, account }) => {
  await request.post(`/api/accounts/${account.id}/orders`, { data: { symbol: "MKTX", side: "buy", qty: 1 } });
  await open();
  await page.getByRole("button", { name: "Menu" }).tap();
  await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Data", exact: true }).tap();
  const dialog = page.getByRole("dialog", { name: "Data and backups" });
  await expect(dialog.getByTestId("save-status")).toContainText("Saving works");
  await dialog.getByRole("button", { name: "Back up now" }).tap();
  await expect(page.getByText(/Backup saved/)).toBeVisible();
  expect(await smallIn(page, ".data-modal")).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await dialog.getByTestId("backup-list").getByRole("listitem").first().getByRole("button", { name: "Restore…" }).tap();
  const preview = dialog.getByRole("region", { name: "What the restore changes" });
  await expect(preview).toContainText("Nothing has changed yet");
  expect(await smallIn(page, ".data-modal")).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
  await preview.getByTestId("restore-apply").tap();
  await expect(page.getByText("Backup restored")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).tap();
  await expect(dialog).toHaveCount(0);
});

test("the recovery screen fits a phone", async ({ page }) => {
  await page.route("**/api/data/status", (r) => r.fulfill({ json: { recovery: { kind: "newer", reason: "state.json it was written by a newer PaperDesk (state format version 9; this build reads up to version 2)", copy: null } } }));
  await page.route("**/api/data/recovery", (r) => r.fulfill({ json: { recovery: {}, backups: [] } }));
  await page.goto("/");
  const card = page.getByTestId("recovery");
  await expect(card.getByRole("heading", { name: "Saved data is from a newer PaperDesk" })).toBeVisible();
  await expect(card.getByRole("button", { name: "No backups to restore" })).toBeDisabled();
  expect(await smallIn(page, "[data-testid=recovery]")).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
});
