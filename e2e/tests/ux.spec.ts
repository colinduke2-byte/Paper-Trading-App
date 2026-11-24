import { test, expect, createAccount, ticket, bottomTab, review, place, confirmDialog } from "./fixtures";

// UX review follow-ups (docs/reviews/UX_REVIEW_2026-11-24.md): connection state, layout at common laptop
// sizes, time zone labels in a replay, the first-run card and number formatting.

test("a lost live connection shows a bar, dims the numbers, blocks the ticket and recovers", async ({ page, open }) => {
  // Route the live stream through the test so it can be cut and restored without stopping the server.
  let allow = true;
  const live: { close: () => Promise<void> }[] = [];
  await page.routeWebSocket(/\/ws$/, (ws) => {
    if (!allow) { ws.close({ code: 1011, reason: "test: server unreachable" }); return; }
    ws.connectToServer();
    live.push(ws);
  });
  await open();
  await expect(page.locator(".top-right .dot.ok")).toBeVisible();
  await expect(page.locator(".top-right .muted")).toContainText(/ · \d{2}:\d{2}/); // the first tick has arrived
  await expect(page.locator(".lost-bar")).toHaveCount(0);

  allow = false;
  for (const ws of live.splice(0)) await ws.close();
  const bar = page.getByRole("alert").filter({ hasText: "Connection to the PaperDesk server lost" });
  await expect(bar).toBeVisible({ timeout: 8000 });
  await expect(bar).toContainText(/frozen as of \d{2}:\d{2}:\d{2} \S+/);
  await expect(page.locator(".app")).toHaveClass(/stale/);
  // The ticket's buttons do not take clicks while the numbers are frozen.
  await expect(ticket(page).getByRole("button", { name: /^Review/ })).toHaveCSS("pointer-events", "none");

  allow = true;
  await expect(bar).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText("Reconnected. Prices and balances are live again.")).toBeVisible();
  await expect(page.locator(".app")).not.toHaveClass(/stale/);
});

for (const [w, h] of [[1280, 720], [1024, 640]] as const) {
  test(`at ${w}x${h} the chart stays above the bottom tabs and every tab can be clicked`, async ({ page, open }) => {
    await page.setViewportSize({ width: w, height: h });
    await open();
    await expect(page.locator(".ct-stage[data-loaded='1']")).toBeVisible();
    const mv = (await page.locator(".main-view").boundingBox())!;
    const bottom = (await page.locator(".bottom").boundingBox())!;
    expect(mv.y + mv.height, "the chart panel ends above the bottom panel").toBeLessThanOrEqual(bottom.y + 1);
    for (const tab of ["Orders", "Fills", "Activity", "Reports", "Alerts", "Screener", "Positions"]) {
      await bottomTab(page, new RegExp(`^${tab}`)).click(); // fails if anything covers the tab
      await expect(bottomTab(page, new RegExp(`^${tab}`))).toHaveAttribute("aria-selected", "true");
    }
    // No page scroll is needed to reach the order ticket.
    expect(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight)).toBeLessThanOrEqual(1);
    const t = (await ticket(page).boundingBox())!;
    expect(t.y, "the order ticket starts on screen").toBeLessThan(h);
    await review(page, "1");
    await expect(ticket(page).getByRole("button", { name: "Place paper order" })).toBeVisible();
  });
}

test("in a replay every time is labelled New York time (ET)", async ({ page, open, request }) => {
  await open();
  try {
    await page.getByRole("button", { name: "Replay", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "Replay a trading session" });
    await expect(picker).toContainText("New York time (ET)");
    await picker.getByRole("button", { name: /^Start with/ }).click();
    const bar = page.getByRole("region", { name: "Replay controls" });
    await expect(bar.locator(".replay-clock")).toContainText(/\d{2}:\d{2}:\d{2} ET/);
    // The header shows no second, unlabelled clock during a replay.
    await expect(page.locator(".top-right .muted")).toHaveText(/^(Market open|Market closed|Pre-market|After hours)$/);

    await review(page, "1");
    await place(page);
    await expect(page.getByText(/Filled: buy 1 MKTX/)).toBeVisible();
    await bottomTab(page, "Fills").click();
    const fills = page.locator(".bottom table");
    await expect(fills.locator("th").first()).toHaveText("Time (ET)");
    const clock = (await bar.locator(".replay-clock").innerText()).slice(0, 5); // HH:MM in ET
    await expect(fills.locator("tbody tr").first().locator("td").first()).toContainText(clock);
    // The chart legend uses the same clock.
    await expect(page.getByTestId("chart-legend")).toContainText(clock);

    await bar.getByRole("button", { name: "Exit replay" }).click();
    await confirmDialog(page, "Keep replaying");
    await expect(bar).toBeVisible();
    await bar.getByRole("button", { name: "Exit replay" }).click();
    await confirmDialog(page, "Exit replay");
    await expect(bar).toHaveCount(0);
    await expect(page.locator(".top-right .muted")).toContainText(/Market open · \d{2}:\d{2}:\d{2} \S+/);
  } finally {
    await request.post("/api/replay/exit").catch(() => {});
  }
});

test("the first-run welcome card starts a first trade and stays dismissed", async ({ page, request }) => {
  const acc = await createAccount(request);
  await page.addInitScript((a) => {
    if (!localStorage.getItem("pd.account")) { localStorage.setItem("pd.account", a); localStorage.setItem("pd.symbol", "TECX"); }
  }, acc.id);
  await page.goto("/");
  const card = page.getByRole("region", { name: "Welcome to PaperDesk" });
  await expect(card).toBeVisible();
  await expect(card).toContainText("pretend money");
  await expect(card).toContainText("simulated");
  await card.getByRole("button", { name: "Buy 10 shares of MKTX" }).click();
  await expect(card).toHaveCount(0);
  const shares = ticket(page).getByLabel("Shares");
  await expect(shares).toHaveValue("10");
  await expect(shares).toBeFocused();
  await expect(page.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Welcome to PaperDesk" })).toHaveCount(0);
});

test("zero amounts never show a minus sign", async ({ page, open }) => {
  await open();
  await expect(page.locator(".top-stats")).toContainText("$0.00"); // Day P&L of a new account, with its currency
  await bottomTab(page, "Reports").click();
  await page.locator(".reports-nav").getByRole("button", { name: "Statements" }).click();
  const fees = page.locator(".rep-summary tr").filter({ hasText: "Fees" }).locator("td.num");
  await expect(fees).toHaveText("$0.00");
  await page.locator(".reports-nav").getByRole("button", { name: "Overview" }).click();
  await expect(page.locator(".rep-body .facts").first()).toBeVisible();
  for (const where of [".reports", ".top-stats"]) {
    const text = await page.locator(where).innerText();
    expect(text, `${where} has no negative zero`).not.toMatch(/[-−]\$?0\.00(?!\d)/);
  }
});

test("restoring default rules and leaving with unsaved changes both ask first", async ({ page, open }) => {
  await open();
  await page.getByRole("button", { name: "Account rules" }).click();
  const dlg = page.getByRole("dialog", { name: /Account rules/ });
  const minimum = dlg.getByLabel("Pattern day trader minimum account value");
  await minimum.fill("30000");
  await dlg.getByRole("button", { name: "Cancel" }).click();
  await confirmDialog(page, "Keep editing");
  await expect(minimum).toHaveValue("30000");
  await dlg.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(/^Rules saved for /)).toBeVisible();
  await dlg.getByRole("button", { name: "Restore default rules" }).click();
  await confirmDialog(page, "Restore default rules");
  await expect(minimum).toHaveValue("25000");
  await expect(page.getByText("Default rules restored.")).toBeVisible();
  await dlg.getByRole("button", { name: "Close" }).click();
  await expect(dlg).toHaveCount(0);
});

test("the option chain opens at the money with a spot divider", async ({ page, open }) => {
  await open();
  await page.locator(".main-view").getByRole("tab", { name: "Options" }).click();
  const spot = page.locator("tr.spot-row");
  await expect(spot).toHaveCount(1);
  await expect(spot).toContainText(/last \$\d/);
  await expect(spot).toBeInViewport();
});
