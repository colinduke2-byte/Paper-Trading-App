import { test, expect, ticket, bottomTab } from "./fixtures";
import { threeLots } from "./lots-helpers";

// Tax lots on the desktop layout: view the open lots, sell two chosen lots from the order ticket,
// and see the chosen lots in the realised gains report.

test("view lots and sell two chosen lots", async ({ page, open, account, request }) => {
  await threeLots(request, account.id);
  await open();
  await bottomTab(page, "Lots").click();
  const lots = page.getByRole("region", { name: /Lots of MKTX/ });
  await expect(lots.locator(".lot-row")).toHaveCount(3);
  await expect(lots).toContainText("L1");
  await expect(lots).toContainText("long-term on");
  await lots.getByLabel("Select lot L2").check();
  await lots.getByLabel("Select lot L3").check();
  await lots.getByRole("button", { name: "Sell selected lots (2)" }).click();

  const t = ticket(page);
  await expect(t.getByLabel("Shares")).toHaveValue("10");
  await expect(t.getByText("Choose tax lots (optional) · 2 chosen")).toBeVisible();
  await expect(t.getByLabel("Close lot L2")).toBeChecked();
  await expect(t.getByLabel("Close lot L1")).not.toBeChecked();
  await t.getByRole("button", { name: /^Review/ }).click();
  await expect(t.getByText(/Estimated gain/).first()).toBeVisible();
  await t.getByRole("button", { name: "Place paper order" }).click();
  await expect(page.getByText(/Filled: sell 10 MKTX/)).toBeVisible();

  const after = await (await request.get(`/api/accounts/${account.id}/lots`)).json();
  expect(after.positions[0].lots.map((l: any) => l.id)).toEqual(["L1"]);
  await expect(lots.locator(".lot-row")).toHaveCount(1);

  await bottomTab(page, "Reports").click();
  await page.getByRole("button", { name: "Gains", exact: true }).click();
  const rows = page.locator(".reports tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.filter({ hasText: "L2" })).toContainText("Chosen lot");
  await expect(rows.filter({ hasText: "L3" })).toContainText("Chosen lot");
  await page.getByRole("button", { name: "Tax year", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Tax year" })).toBeVisible();
  await expect(page.getByText("Informational only, not tax advice.").first()).toBeVisible();
});

test("a lot named by an open order cannot be named again", async ({ page, open, account, request }) => {
  await threeLots(request, account.id);
  const r = await request.post(`/api/accounts/${account.id}/orders`, {
    data: { symbol: "MKTX", side: "sell", qty: 5, type: "limit", limit_price: "99999", tif: "gtc", lots: [{ lot_id: "L1", qty: 5 }] },
  });
  expect(r.ok()).toBeTruthy();
  await open();
  await bottomTab(page, "Lots").click();
  const lots = page.getByRole("region", { name: /Lots of MKTX/ });
  await expect(lots).toContainText("5 named by an open order");
  await expect(lots.getByLabel("Select lot L1")).toBeDisabled();
});
