import { test, expect, ticket } from "./fixtures";
import type { Locator, Page } from "@playwright/test";

// Keyboard only (desktop layout): the main trading path without a mouse. Tab order runs top bar, watchlist,
// main view, order ticket; every focused control shows a focus ring; dialogs take focus, keep Tab inside,
// close on Escape and give focus back to the control that opened them.

type Stop = { desc: string; ring: boolean };

async function focused(page: Page): Promise<Stop> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return { desc: "body", ring: true };
    const cs = getComputedStyle(el);
    const ring = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== "none";
    const name = el.getAttribute("aria-label") || (el as HTMLInputElement).labels?.[0]?.textContent || el.textContent || el.getAttribute("placeholder") || "";
    return { desc: `${el.tagName.toLowerCase()} "${name.trim().replace(/\s+/g, " ").slice(0, 40)}"`, ring };
  });
}

// Press Tab until `target` has focus; returns every stop on the way.
async function tabTo(page: Page, target: Locator, max = 120, back = false): Promise<Stop[]> {
  const stops: Stop[] = [];
  for (let i = 0; i < max; i++) {
    await page.keyboard.press(back ? "Shift+Tab" : "Tab");
    stops.push(await focused(page));
    if (await target.evaluate((el) => el === document.activeElement)) return stops;
  }
  throw new Error(`Tab never reached the target. Stops: ${stops.map((s) => s.desc).join(", ")}`);
}

const insideDialog = (page: Page) => page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'));

test("the trading path works with the keyboard alone", async ({ page, open }) => {
  await open();
  await page.locator("body").focus();
  const t = ticket(page);
  const account = page.getByRole("combobox", { name: "Account" });
  const search = page.getByPlaceholder("Search symbol or company");
  const shares = t.getByLabel("Shares");

  const toAccount = await tabTo(page, account);
  const toSearch = await tabTo(page, search);
  const toShares = await tabTo(page, shares);
  const all = [...toAccount, ...toSearch, ...toShares];
  expect(all.filter((s) => !s.ring).map((s) => s.desc), "focused controls without a visible focus ring").toEqual([]);
  expect(all.map((s) => s.desc)).not.toContain("body");

  // Pick a symbol from the search box with Enter.
  await search.focus();
  await page.keyboard.type("TECX");
  await expect(page.locator(".results")).toContainText("TECX");  // results arrive from the server
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "TECX", level: 2 })).toBeVisible();

  // Order: shares, review with Enter on the button, place with Enter.
  await shares.focus();
  await page.keyboard.type("2");
  const reviewBtn = t.getByRole("button", { name: /^Review/ });
  await tabTo(page, reviewBtn, 20);
  await page.keyboard.press("Enter");
  const placeBtn = t.getByRole("button", { name: "Place paper order" });
  await expect(placeBtn).toBeVisible();
  await placeBtn.focus();
  expect((await focused(page)).ring, "Place paper order shows a focus ring").toBe(true);
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Filled: buy 2 TECX/)).toBeVisible();
});

for (const which of [
  { button: "New account", dialog: "New paper account" },
  { button: "Account rules", dialog: "Account rules" },
  { button: "Replay", dialog: "Replay a trading session" },
]) {
  test(`the ${which.button} dialog traps focus, closes on Escape and restores focus`, async ({ page, open }) => {
    await open();
    await page.locator("body").focus();
    const opener = page.getByRole("button", { name: which.button, exact: true });
    await tabTo(page, opener);
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: which.dialog });
    await expect(dialog).toBeVisible();
    await expect.poll(() => insideDialog(page), "focus moves into the dialog").toBe(true);
    for (let i = 0; i < 25; i++) {
      await page.keyboard.press("Tab");
      expect(await insideDialog(page), `Tab ${i + 1} stays inside the dialog`).toBe(true);
      expect((await focused(page)).ring, `Tab ${i + 1} shows a focus ring: ${(await focused(page)).desc}`).toBe(true);
    }
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press("Shift+Tab");
      expect(await insideDialog(page), `Shift+Tab ${i + 1} stays inside the dialog`).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
  });
}
