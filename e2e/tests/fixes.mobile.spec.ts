import { test, expect, createAccount, lastPrice, bottomTab } from "./fixtures";

test("phone: change a working order in a dialog that closes with Escape", async ({ page, request }) => {
  const acc = await createAccount(request, "margin", "10000");
  const px = await lastPrice(request, "MKTX");
  const limit = Math.max(1, Math.floor(px * 0.5));
  expect((await request.post(`/api/accounts/${acc.id}/orders`, { data: { symbol: "MKTX", side: "buy", qty: "2", type: "limit", limit_price: String(limit), tif: "gtc" } })).ok()).toBeTruthy();
  await page.addInitScript((a) => { localStorage.setItem("pd.account", a); localStorage.setItem("pd.symbol", "MKTX"); }, acc.id);
  await page.goto("/");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Account", exact: true }).tap();
  await bottomTab(page, /^Orders/).tap();
  await page.locator(".bottom tbody tr").filter({ hasText: "Working" }).getByRole("button", { name: /^Change order/ }).tap();
  const dialog = page.getByRole("dialog", { name: /^Change order/ });
  const input = dialog.getByLabel("Quantity");
  expect(await input.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
  for (const b of await dialog.getByRole("button").all()) {
    const box = (await b.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(40);
  }
  await input.fill("4");
  await dialog.getByRole("button", { name: "Change order" }).tap();
  await expect(page.getByText(/Order changed: buy 4 MKTX/)).toBeVisible();
  await page.getByRole("button", { name: "Menu" }).tap();
  await expect(page.getByRole("dialog", { name: "Menu" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Menu" })).toHaveCount(0);
});
