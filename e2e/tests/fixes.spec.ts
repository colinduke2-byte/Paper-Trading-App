import { test, expect, createAccount, lastPrice, bottomTab } from "./fixtures";

const topButton = (page: any, name: string) => page.locator("header").getByRole("button", { name, exact: true });

// The browser logs a refused request (HTTP 400 or 422) as a failed resource load; those entries are expected.
function expectRefusals(problems: string[], status: number, n: number) {
  const found = problems.filter((p) => new RegExp(`Failed to load resource: .* ${status}`).test(p));
  expect(found).toHaveLength(n);
  for (const f of found) problems.splice(problems.indexOf(f), 1);
}

test("dialogs close with Escape and give focus back to the button that opened them", async ({ page, open }) => {
  await open();
  for (const [button, title] of [["New account", "New paper account"], ["Settings", "Account rules"], ["Replay", "Replay a trading session"], ["Help", "Help"]]) {
    await topButton(page, button).click();
    const dialog = page.getByRole("dialog", { name: title });
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    // Tab stays inside the dialog
    for (let i = 0; i < 12; i++) await page.keyboard.press("Tab");
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(topButton(page, button)).toBeFocused();
  }
});

test("the reset dialog ignores a backdrop click; the new-account dialog closes on one", async ({ page, open }) => {
  await open();
  await topButton(page, "Reset").click();
  const reset = page.getByRole("dialog", { name: /^Reset/ });
  await page.mouse.click(5, 5);
  await expect(reset).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(reset).toHaveCount(0);
  await topButton(page, "New account").click();
  await page.mouse.click(5, 5);
  await expect(page.getByRole("dialog", { name: "New paper account" })).toHaveCount(0);
});

test("starting cash typed with thousands separators is accepted", async ({ page, open, request, problems }) => {
  await open();
  await topButton(page, "New account").click();
  const dialog = page.getByRole("dialog", { name: "New paper account" });
  const name = `Commas ${Date.now()}`;
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Starting cash").fill("12,500");
  await dialog.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText(`Account "${name}" created`)).toBeVisible();
  const accounts = await (await request.get("/api/accounts")).json();
  expect(accounts.find((a: any) => a.name === name).starting_cash).toBe(12500);
  // an unclear number gets the server's message, not a server error
  await topButton(page, "New account").click();
  await dialog.getByLabel("Starting cash").fill("1,5");
  await dialog.getByRole("button", { name: "Create account" }).click();
  await expect(dialog.getByRole("alert")).toContainText("starting cash is not a number");
  expectRefusals(problems, 400, 1);
});

test("a working order can be changed from the Orders tab, and a refusal shows the reason", async ({ page, request, problems }) => {
  const acc = await createAccount(request, "margin", "10000");
  const px = await lastPrice(request, "MKTX");
  const limit = Math.max(1, Math.floor(px * 0.9));
  const r = await request.post(`/api/accounts/${acc.id}/orders`, { data: { symbol: "MKTX", side: "buy", qty: "2", type: "limit", limit_price: String(limit), tif: "gtc" } });
  expect(r.ok()).toBeTruthy();
  await page.addInitScript((a) => { localStorage.setItem("pd.account", a); localStorage.setItem("pd.symbol", "MKTX"); }, acc.id);
  await page.goto("/");
  await bottomTab(page, /^Orders/).click();
  const row = page.locator(".bottom tbody tr").filter({ hasText: "Working" });
  await row.getByRole("button", { name: /^Change order/ }).click();
  const dialog = page.getByRole("dialog", { name: /^Change order/ });
  await dialog.getByLabel("Quantity").fill("3");
  await dialog.getByLabel("Limit price").fill(String(limit - 1));
  await dialog.getByRole("button", { name: "Change order" }).click();
  await expect(page.getByText(/Order changed: buy 3 MKTX/)).toBeVisible();
  await expect(row.getByRole("cell").nth(3)).toHaveText("3");
  await row.getByRole("button", { name: /^Change order/ }).click();
  await dialog.getByLabel("Quantity").fill("1,000,000");
  await dialog.getByRole("button", { name: "Change order" }).click();
  await expect(dialog.getByRole("alert")).toContainText("The order was not changed: insufficient buying power for the change");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expectRefusals(problems, 422, 1);
});

test("chart timeframes read 1m to 1W and have spoken names", async ({ page, open }) => {
  await open();
  const tfs = page.getByRole("group", { name: "Timeframe" });
  for (const [text, name] of [["1m", "1 minute"], ["5m", "5 minutes"], ["15m", "15 minutes"], ["1h", "1 hour"], ["1D", "1 day"], ["1W", "1 week"]]) {
    await expect(tfs.getByRole("button", { name, exact: true })).toHaveText(text);
  }
});

test("reports label prices like the banner", async ({ page, open }) => {
  await open();
  await bottomTab(page, "Reports").click();
  await expect(page.locator(".rep-label")).toHaveText("All figures use simulated prices.");
});
