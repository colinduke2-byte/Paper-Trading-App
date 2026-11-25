import { test, expect, lastPrice, bottomTab } from "./fixtures";

// Alerts and the notification centre on the desktop layout (app/alerts, AlertsTab.tsx, Notifications.tsx).

test("a price alert fires, the bell counts it and marking it read clears the count", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  await bottomTab(page, "Alerts").click();
  await expect(page.locator(".bottom .scroll")).toContainText("No price alerts.");
  await page.getByLabel("Alert symbol").fill("MKTX");
  await page.getByLabel("Condition").selectOption("above");
  await page.getByLabel("Alert price").fill((px / 2).toFixed(2)); // already above: fires on the next tick
  await page.getByRole("button", { name: "Add price alert" }).click();

  const bell = page.locator(".top").getByRole("button", { name: /^Notifications/ });
  await expect(bell).toHaveAccessibleName("Notifications (1 unread)");
  await expect(page.locator(".toasts")).toContainText("Alert: MKTX: MKTX rises to");

  await bell.click();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await expect(panel.locator(".notif.unread")).toHaveCount(1);
  await expect(panel).toContainText(`MKTX rises to ${(px / 2).toFixed(2)}`);
  await panel.getByRole("button", { name: "Mark all read" }).click();
  await expect(panel.locator(".notif.unread")).toHaveCount(0);
  await panel.getByRole("button", { name: "Close" }).click();
  await expect(bell).toHaveAccessibleName("Notifications");

  // The fired alert moved to the finished list.
  await expect(page.locator(".bottom .scroll")).toContainText("No active alerts.");
  await page.getByLabel("Show alerts").selectOption("all");
  await expect(page.locator(".alerts-table tbody tr")).toContainText("Triggered");

  // It is still there after a reload (the log is served by the API for catch-up).
  await page.reload();
  await page.locator(".top").getByRole("button", { name: "Notifications" }).click();
  await expect(page.getByRole("dialog", { name: "Notifications" }).locator(".notif")).toHaveCount(1);
});

test("the builder creates other alert types, and alerts can be paused, edited and deleted", async ({ page, open }) => {
  await open();
  await bottomTab(page, "Alerts").click();
  await page.getByRole("button", { name: "New alert…" }).click();
  const dlg = page.getByRole("dialog", { name: "New alert" });
  await dlg.getByLabel("Alert type").selectOption("change");
  await dlg.getByLabel("Symbol").fill("TECX");
  await dlg.getByLabel("Condition").selectOption("below");
  await dlg.getByLabel("Percent change", { exact: true }).fill("-40");
  await dlg.getByLabel("Re-arm after it fires").check();
  await dlg.getByRole("button", { name: "Create alert" }).click();
  await expect(dlg).toHaveCount(0);
  const row = page.locator(".alerts-table tbody tr").filter({ hasText: "TECX change on the day falls to -40.00%" });
  await expect(row).toContainText("Active");
  await expect(row).toContainText("Re-arms after 15 min cool-down");

  await row.getByRole("button", { name: "Pause" }).click();
  await expect(row).toContainText("Paused");
  await row.getByRole("button", { name: "Resume" }).click();
  await expect(row).toContainText("Active");

  await row.getByRole("button", { name: "Edit" }).click();
  const edit = page.getByRole("dialog", { name: "Edit alert" });
  await edit.getByLabel("Percent change", { exact: true }).fill("-45");
  await edit.getByRole("button", { name: "Save alert" }).click();
  const edited = page.locator(".alerts-table tbody tr").filter({ hasText: "falls to -45.00%" });
  await expect(edited).toBeVisible();

  await page.getByRole("button", { name: "New alert…" }).click();
  await dlg.getByLabel("Alert type").selectOption("indicator");
  await dlg.getByLabel("Symbol").fill("MKTX");
  await dlg.getByRole("combobox", { name: "Bars", exact: true }).selectOption("1h");
  await dlg.getByRole("button", { name: "Create alert" }).click();
  await expect(page.locator(".alerts-table tbody tr").filter({ hasText: "MKTX closes above its 20-bar SMA (hourly bars, crossing)" })).toBeVisible();

  await edited.getByRole("button", { name: "Delete" }).click();
  await expect(edited).toHaveCount(0);
});

test("alert me at this price opens the builder filled in", async ({ page, open, request }) => {
  await open();
  const px = await lastPrice(request, "MKTX");
  const target = (px * 1.25).toFixed(2);
  await page.evaluate(([p]) => window.dispatchEvent(new CustomEvent("pd:alert-at-price", { detail: { symbol: "MKTX", price: Number(p) } })), [target]);
  const dlg = page.getByRole("dialog", { name: "New alert" });
  await expect(dlg.getByLabel("Symbol")).toHaveValue("MKTX");
  await expect(dlg.getByLabel("Price", { exact: true })).toHaveValue(target);
  await expect(dlg.getByLabel("Condition")).toHaveValue("above");
  await dlg.getByRole("button", { name: "Create alert" }).click();
  await expect(page.locator(".toasts")).toContainText(`Alert created: MKTX rises to ${target}`);
});

test("delivery settings ask for browser permission and keep per-type toggles", async ({ page, open }) => {
  // Headless Chromium always reports notifications as denied, so a stand-in for the browser API answers the prompt.
  await page.addInitScript(() => {
    class FakeNotification { static permission = "default"; static async requestPermission() { FakeNotification.permission = "granted"; return "granted"; } }
    Object.defineProperty(window, "Notification", { value: FakeNotification, configurable: true });
  });
  await open();
  await page.locator(".top").getByRole("button", { name: /^Notifications/ }).click();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await expect(panel).toContainText("No notifications yet.");
  await panel.getByRole("button", { name: "Delivery" }).click();
  await panel.getByRole("button", { name: "Allow browser notifications…" }).click();
  await expect(panel.getByLabel("Browser notifications while PaperDesk is in the background")).toBeChecked();
  await panel.getByLabel("Dividends").uncheck();
  await panel.getByRole("button", { name: "Close" }).click();
  const prefs = await page.evaluate(() => JSON.parse(localStorage.getItem("pd.notify") || "{}"));
  expect(prefs).toMatchObject({ browser: true, off: ["dividend"] });
});

test("over plain HTTP from another device the panel says browser notifications are unavailable", async ({ page, open }) => {
  // A phone on the LAN opens http://192.168.x.x: not a secure context. Emulate that here.
  await page.addInitScript(() => Object.defineProperty(window, "isSecureContext", { get: () => false }));
  await open();
  await page.locator(".top").getByRole("button", { name: /^Notifications/ }).click();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await panel.getByRole("button", { name: "Delivery" }).click();
  await expect(panel.getByRole("note")).toContainText("Browser notifications need a secure connection");
  await expect(panel.getByRole("button", { name: "Allow browser notifications…" })).toHaveCount(0);
  await expect(panel.getByLabel("Play a sound")).toBeVisible();
});
