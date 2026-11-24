import { test, expect, createAccount, lastPrice, ticket, confirmDialog, type Account } from "./fixtures";
import type { APIRequestContext, Locator, Page } from "@playwright/test";

// Chart tools: chart types, indicators, drawings, order lines, trade at price and keyboard shortcuts.
// Chart settings and drawings are stored per server, so every test starts from a clean chart state.
// Leaving the page flushes any pending save (pagehide), so leave it and only then reset, before the next test.
test.beforeEach(async ({ request }) => { await request.delete("/api/chartstate"); });
test.afterEach(async ({ page, request }) => {
  await page.goto("/api/status").catch(() => {}); // a same-origin page without the app
  await page.waitForTimeout(500);
  await request.delete("/api/chartstate");
});

const chartImg = (page: Page, sym = "MKTX") => page.getByRole("img", { name: `${sym} price chart` });
const legend = (page: Page) => page.getByTestId("chart-legend");
const stage = (page: Page) => page.locator(".ct-stage");

async function chartReady(page: Page) {
  await expect(stage(page)).toHaveAttribute("data-loaded", "1");
  await expect(legend(page)).toBeVisible();
  await expect(page.getByText(/^(Market open|Market closed|Pre-market|After hours) · /)).toBeVisible(); // the header has its final size
}

/** The element's box once it has stopped moving (the price scale settles after the data and order lines load). */
async function stableBox(pageOrLoc: Page | Locator, maybeLoc?: Locator) {
  const loc = (maybeLoc ?? pageOrLoc) as Locator;
  let prev = await loc.boundingBox();
  for (let i = 0; i < 20; i++) {
    await loc.page().waitForTimeout(250);
    const cur = await loc.boundingBox();
    if (prev && cur && Math.abs(prev.y - cur.y) < 0.5) return cur;
    prev = cur;
  }
  return prev!;
}

async function order(request: APIRequestContext, acc: Account, body: Record<string, unknown>) {
  const r = await request.post(`/api/accounts/${acc.id}/orders`, { data: { symbol: "MKTX", tif: "day", ...body } });
  expect(r.ok(), await r.text()).toBeTruthy();
  return (await r.json())[0];
}

async function getOrder(request: APIRequestContext, acc: Account, id: string) {
  const r = await request.get(`/api/accounts/${acc.id}/orders`);
  return (await r.json()).find((o: any) => o.id === id);
}

test("chart type can be switched and is remembered", async ({ page, open }) => {
  await open();
  await chartReady(page);
  const type = page.getByLabel("Chart type");
  for (const t of ["bars", "line", "area", "candles", "line"]) {
    await type.selectOption(t);
    await expect(stage(page)).toHaveAttribute("data-chart-type", t);
    await expect(chartImg(page).locator("canvas").first()).toBeVisible();
    await expect(legend(page)).toContainText("MKTX");
  }
  await page.getByRole("button", { name: "Log" }).click();
  await expect(stage(page)).toHaveAttribute("data-log", "1");
  await page.reload();
  await chartReady(page);
  await expect(type).toHaveValue("line");
  await expect(stage(page)).toHaveAttribute("data-log", "1");
  await page.getByRole("button", { name: "Reset chart view" }).click();
});

test("legend shows the bar's prices, change and volume", async ({ page, open }) => {
  await open();
  await chartReady(page);
  await expect(legend(page)).toContainText(/O \d/);
  await expect(legend(page)).toContainText(/H \d/);
  await expect(legend(page)).toContainText(/L \d/);
  await expect(legend(page)).toContainText(/C \d/);
  await expect(legend(page)).toContainText(/Vol /);
  await expect(legend(page)).toContainText(/\([+-]?\d+\.\d\d%\)/);
  await expect(legend(page)).toContainText("SMA 20");
  // Hovering an earlier bar changes the date and time shown.
  const before = await legend(page).locator(".ct-legend-row").first().textContent();
  const box = (await chartImg(page).boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.3, box.y + 60);
  await expect.poll(async () => legend(page).locator(".ct-legend-row").first().textContent()).not.toBe(before);
});

test("indicators follow the streaming last price between reloads", async ({ page, open }) => {
  await open();
  await chartReady(page);
  // A 2-bar average moves by half of every price change, so it shows at two decimals within seconds.
  await page.getByRole("button", { name: /^Indicators/ }).click();
  await page.getByRole("dialog", { name: "Indicators" }).getByLabel("SMA period").fill("2");
  await page.getByLabel("Close indicators").click();
  await expect(legend(page)).toContainText("SMA 2 ");
  await page.mouse.move(5, 5); // the legend shows the latest bar while the pointer is off the chart
  const read = async () => {
    const t = (await legend(page).textContent())!;
    return { close: t.match(/C ([\d,.]+)/)![1], sma: t.match(/SMA 2 ([\d,.]+)/)![1] };
  };
  const first = await read();
  // Bars reload only every 60 seconds; within 20 seconds both values must have moved with the stream.
  await expect.poll(async () => { const r = await read(); return r.close !== first.close && r.sma !== first.sma; }, { timeout: 20_000 }).toBe(true);
});

test("an indicator can be added, its period changed, and removed", async ({ page, open }) => {
  await open();
  await chartReady(page);
  await page.getByRole("button", { name: /^Indicators/ }).click();
  const panel = page.getByRole("dialog", { name: "Indicators" });
  await panel.getByLabel("Add indicator").selectOption("rsi");
  await expect(legend(page)).toContainText(/RSI 14 \d/);
  await panel.getByLabel("RSI period").fill("9");
  await expect(legend(page)).toContainText(/RSI 9 \d/);
  await expect(legend(page)).not.toContainText("RSI 14");
  for (const t of ["macd", "atr", "stoch", "volma", "ema", "bb"]) await panel.getByLabel("Add indicator").selectOption(t);
  await expect(legend(page)).toContainText("MACD 12 26 9");
  await expect(legend(page)).toContainText("ATR 14");
  await expect(legend(page)).toContainText("Stochastic 14 3 3");
  await expect(legend(page)).toContainText("Volume MA 20");
  // The settings are saved on the server, so a reload (or another browser) keeps them.
  await page.reload();
  await chartReady(page);
  await expect(legend(page)).toContainText(/RSI 9 \d/);
  await page.getByRole("button", { name: /^Indicators/ }).click();
  await panel.getByRole("button", { name: "Remove RSI 9" }).click();
  await expect(legend(page)).not.toContainText("RSI");
  await expect(legend(page)).toContainText("MACD 12 26 9");
});

test("a horizontal line stays after a reload, a timeframe change and in another browser", async ({ page, open, browser, account }) => {
  await open();
  await chartReady(page);
  await page.getByRole("button", { name: "Draw", exact: true }).click();
  await page.getByRole("button", { name: "Horizontal line" }).click();
  const layer = page.getByTestId("draw-layer");
  const box = (await layer.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.getByRole("button", { name: /^Drawings/ }).click();
  const list = page.getByRole("dialog", { name: "Drawings" });
  const item = list.getByRole("button", { name: /^Horizontal line at \d/ });
  await expect(item).toHaveCount(1);
  const text = await item.textContent();

  const check = async (p: Page) => {
    await p.getByRole("button", { name: "Draw", exact: true }).click();
    await p.getByRole("button", { name: /^Drawings \(1\)/ }).click();
    await expect(p.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: text!, exact: true })).toBeVisible();
  };
  await page.reload();
  await chartReady(page);
  await check(page);
  await page.getByRole("group", { name: "Timeframe" }).getByRole("button", { name: "1H", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: text!, exact: true })).toBeVisible();
  await page.getByRole("group", { name: "Timeframe" }).getByRole("button", { name: "1D", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: text!, exact: true })).toBeVisible();

  // A fresh browser profile has no local cache: the drawing comes from the server.
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, baseURL: new URL(page.url()).origin });
  const other = await ctx.newPage();
  const errors: string[] = [];
  other.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await other.addInitScript((a) => localStorage.setItem("pd.account", a), account.id);
  await other.goto("/");
  await expect(other.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
  await check(other);
  // Delete it there; the original page sees it gone after a reload.
  await other.getByRole("dialog", { name: "Drawings" }).getByRole("button", { name: `Remove ${text}` }).click();
  await expect(other.getByRole("button", { name: /^Drawings \(0\)/ })).toBeVisible();
  await expect.poll(async () => (await (await other.request.get("/api/chartstate/drawings/MKTX")).json())?.data?.length).toBe(0);
  await ctx.close();
  expect(errors).toEqual([]);
});

test("drawing tools create, select, move and delete shapes", async ({ page, open }) => {
  await open();
  await chartReady(page);
  await page.getByRole("button", { name: "Draw", exact: true }).click();
  // Measured on every use: the page header can wrap once the clock and account name arrive, moving the chart.
  let box = (await page.getByTestId("draw-layer").boundingBox())!;
  const at = (fx: number, fy: number) => ({ x: box.x + box.width * fx, y: box.y + box.height * fy });
  const remeasure = async () => { box = (await page.getByTestId("draw-layer").boundingBox())!; };
  const click = async (fx: number, fy: number) => { await remeasure(); await page.mouse.click(at(fx, fy).x, at(fx, fy).y); };
  const drag = async (a: { x: number; y: number }, b: { x: number; y: number }) => {
    await page.mouse.move(a.x, a.y); await page.mouse.down(); await page.mouse.move(b.x, b.y, { steps: 6 }); await page.mouse.up();
  };
  await page.getByRole("button", { name: "Trend line" }).click();
  await remeasure(); await drag(at(0.2, 0.3), at(0.4, 0.6));
  await page.getByRole("button", { name: "Ray" }).click();
  // Two taps also draw a two-point shape.
  await click(0.3, 0.5);
  await click(0.5, 0.4);
  await page.getByRole("button", { name: "Rectangle" }).click();
  await remeasure(); await drag(at(0.55, 0.2), at(0.7, 0.4));
  await page.getByRole("button", { name: "Fibonacci retracement" }).click();
  await remeasure(); await drag(at(0.75, 0.8), at(0.9, 0.2));
  await page.getByRole("button", { name: "Vertical line" }).click();
  await click(0.15, 0.5);
  await page.getByRole("button", { name: "Text note" }).click();
  await click(0.1, 0.8);
  await page.getByLabel("Note text").fill("Support test");
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(page.getByRole("button", { name: /^Drawings \(6\)/ })).toBeVisible();

  // The note is selected after it is made; drag it and check its price changed.
  await page.getByRole("button", { name: /^Drawings/ }).click();
  const list = page.getByRole("dialog", { name: "Drawings" });
  const before = await list.getByRole("button", { name: /^Text note "Support test"/ }).textContent();
  await page.getByRole("button", { name: /^Drawings/ }).click();
  await remeasure(); await drag({ x: at(0.1, 0.8).x + 20, y: at(0.1, 0.8).y - 8 }, { x: at(0.1, 0.5).x + 20, y: at(0.1, 0.5).y - 8 });
  await page.getByRole("button", { name: /^Drawings/ }).click();
  await expect(list.getByRole("button", { name: /^Text note "Support test"/ })).not.toHaveText(before!);
  await page.getByRole("button", { name: /^Drawings/ }).click();

  // Clicking empty space clears the selection; picking from the list selects; Delete removes it.
  await click(0.98, 0.02);
  await expect(page.getByRole("button", { name: "Delete selected" })).toBeDisabled();
  await page.getByRole("button", { name: /^Drawings/ }).click();
  await list.getByRole("button", { name: /^Trend line/ }).click();
  await page.getByRole("button", { name: /^Drawings/ }).click();
  await expect(page.getByRole("button", { name: "Delete selected" })).toBeEnabled();
  await page.keyboard.press("Delete");
  await expect(page.getByRole("button", { name: /^Drawings \(5\)/ })).toBeVisible();
  await page.getByRole("button", { name: /^Drawings/ }).click();
  await expect(list.getByRole("button", { name: /^Trend line/ })).toHaveCount(0);
  await expect(list.getByRole("button", { name: /^Ray/ })).toHaveCount(1);
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("draw-layer")).toHaveCount(0);
});

test("dragging an order line asks first: cancel keeps the price, confirm changes it", async ({ page, open, request, account }) => {
  const px = await lastPrice(request, "MKTX");
  const limit = Number((px * 0.99).toFixed(2));
  const o = await order(request, account, { side: "buy", qty: "3", type: "limit", limit_price: limit.toFixed(2) });
  await open();
  await chartReady(page);
  const handle = page.getByRole("button", { name: /^Buy 3 limit at .*Drag/ });
  await expect(handle).toBeVisible();
  const drag = async () => {
    const b = await stableBox(handle);
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - 40, { steps: 8 }); // up: still below the market
    await page.mouse.up();
  };
  await drag();
  const dialog = page.getByRole("dialog", { name: "Confirm order change" });
  await expect(dialog).toContainText(`from ${limit.toFixed(2)} to`);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  expect((await getOrder(request, account, o.id)).limit_price).toBe(limit);
  await expect(handle).toContainText(limit.toFixed(2));

  await drag();
  const to = Number((await dialog.locator("b").nth(2).textContent())!.replace(/,/g, ""));
  expect(to).toBeGreaterThan(limit);
  await dialog.getByRole("button", { name: "Change order" }).click();
  await expect(page.getByText(/^Order changed: Buy 3 limit now at/)).toBeVisible();
  await expect.poll(async () => (await getOrder(request, account, o.id)).limit_price).toBe(to);
  await expect(page.getByRole("button", { name: new RegExp(`^Buy 3 limit at ${to.toFixed(2).replace(".", "\\.")}`) })).toBeVisible();
});

test("a change the server refuses shows the reason and puts the line back", async ({ page, open, request, problems }) => {
  const px = await lastPrice(request, "MKTX");
  const limit = Number((px * 0.99).toFixed(2));
  // A cash account with just enough cash for 2 shares at the limit: raising the price is refused.
  const acc = await createAccount(request, "cash", (2 * limit + 1).toFixed(2));
  const o = await order(request, acc, { side: "buy", qty: "2", type: "limit", limit_price: limit.toFixed(2) });
  await open(acc);
  await chartReady(page);
  const handle = page.getByRole("button", { name: /^Buy 2 limit at .*Drag/ });
  const b = await stableBox(handle);
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2 - 60, { steps: 8 });
  await page.mouse.up();
  await page.getByRole("dialog", { name: "Confirm order change" }).getByRole("button", { name: "Change order" }).click();
  await expect(page.getByText(/^The order was not changed: /)).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Confirm order change" })).toHaveCount(0);
  await expect(handle).toContainText(limit.toFixed(2));
  expect((await getOrder(request, acc, o.id)).limit_price).toBe(limit);
  // The browser logs the refused PATCH (HTTP 422) as a failed resource load; that one entry is expected.
  const expected = problems.filter((p) => /Failed to load resource: .* 422/.test(p));
  expect(expected).toHaveLength(1);
  problems.splice(problems.indexOf(expected[0]), 1);
});

test("multi-leg orders draw no line on the chart", async ({ page, open, request, account }) => {
  const exp = (await (await request.get("/api/options/expirations/MKTX")).json())[0].date;
  const rows = (await (await request.get(`/api/options/chain/MKTX?expiry=${exp}`)).json()).rows;
  const lo = rows[Math.floor(rows.length / 2) - 1], hi = rows[Math.floor(rows.length / 2)];
  const r = await request.post(`/api/accounts/${account.id}/multileg`, { data: {
    qty: "1", type: "limit", limit_price: "0.05", price_effect: "debit", tif: "day",
    legs: [{ symbol: lo.call.symbol, side: "buy", ratio: 1 }, { symbol: hi.call.symbol, side: "sell", ratio: 1 }] } });
  expect(r.ok(), await r.text()).toBeTruthy();
  const combo = (await r.json())[0] ?? (await r.json());
  expect(combo.symbol).toBe("MKTX"); // shown in the chart's order list, so the chart must skip it itself
  expect(["working", "held"]).toContain(combo.status);
  await open();
  await chartReady(page);
  await page.waitForTimeout(1000);
  await expect(page.locator(".ct-order-handle")).toHaveCount(0);
});

test("bracket legs are labelled as take profit and stop loss", async ({ page, open, request, account }) => {
  const px = await lastPrice(request, "MKTX");
  await order(request, account, { side: "buy", qty: "4", type: "market", take_profit: (px * 1.01).toFixed(2), stop_loss: (px * 0.99).toFixed(2) });
  await open();
  await chartReady(page);
  await expect(page.getByRole("button", { name: /^Take profit: Sell 4 limit at/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Stop loss: Sell 4 stop at/ })).toBeVisible();
});

test("trade at price fills in the order ticket from a right-click", async ({ page, open }) => {
  await open();
  await chartReady(page);
  const box = (await chartImg(page).boundingBox())!;
  await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.25, { button: "right" });
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  const first = menu.getByRole("menuitem").first();
  const label = (await first.textContent())!;
  const m = label.match(/^(Buy|Sell) (limit|stop) at ([\d,.]+)$/)!;
  expect(m).not.toBeNull();
  await first.click();
  const t = ticket(page);
  await expect(t.getByLabel("Order type")).toHaveValue(m[2]);
  const priceInput = t.getByLabel(m[2] === "limit" ? "Limit price" : "Stop price");
  await expect.poll(async () => Number(await priceInput.inputValue())).toBe(Number(m[3].replace(/,/g, "")));
  await expect(t.getByRole("button", { name: m[1], exact: true })).toHaveClass(/on/);
  // Nothing is sent: the ticket still needs a quantity and a review.
  await expect(t.getByRole("button", { name: "Place paper order" })).toHaveCount(0);
});

test("compare overlays a second symbol", async ({ page, open }) => {
  await open();
  await chartReady(page);
  await page.getByLabel("Compare with symbol").fill("tecx");
  await page.getByLabel("Compare with symbol").press("Enter");
  await expect(page.getByRole("button", { name: "Stop comparing with TECX" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Log" })).toBeDisabled();
  await page.getByRole("button", { name: "Stop comparing with TECX" }).click();
  await page.getByLabel("Compare with symbol").fill("NOPE");
  await page.getByLabel("Compare with symbol").press("Enter");
  await expect(page.getByText("There is no symbol NOPE to compare with.")).toBeVisible();
});

test("keyboard shortcuts and their help", async ({ page, open }) => {
  await open();
  await chartReady(page);
  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(help).toBeVisible();
  await expect(help).toContainText("Cancel all open orders");
  await page.keyboard.press("Escape");
  await expect(help).toHaveCount(0);
  await page.getByRole("button", { name: "Keyboard shortcuts" }).click(); // the header entry
  await expect(help).toBeVisible();
  await page.keyboard.press("Escape");

  // Typing in a field never triggers a shortcut.
  await page.keyboard.press("/");
  const search = page.getByLabel("Search symbols");
  await expect(search).toBeFocused();
  await page.keyboard.type("?b");
  await expect(search).toHaveValue("?b");
  await expect(help).toHaveCount(0);
  await search.fill("");
  await search.blur();

  await page.keyboard.press("4");
  await expect(page.getByRole("group", { name: "Timeframe" }).getByRole("button", { name: "1H", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("2");
  await expect(page.getByRole("group", { name: "Timeframe" }).getByRole("button", { name: "5M", exact: true })).toHaveAttribute("aria-pressed", "true");
  const t = ticket(page);
  await page.keyboard.press("s");
  await expect(t.getByRole("button", { name: "Sell", exact: true })).toHaveClass(/on/);
  await page.keyboard.press("t");
  await expect(t.getByRole("button", { name: "Buy", exact: true })).toHaveClass(/on/);
  await page.keyboard.press("q");
  await expect(t.getByLabel("Shares")).toBeFocused();
  await page.keyboard.type("2");
  await t.getByLabel("Shares").blur();
  await page.keyboard.press("r");
  await expect(t.getByRole("button", { name: "Place paper order" })).toBeVisible();
  await expect(t.getByRole("button", { name: "Place paper order" })).not.toBeFocused();
  await page.keyboard.press("r");
  await expect(t.getByRole("button", { name: "Place paper order" })).toBeFocused();
  await expect(page.getByText(/^Filled: buy 2/)).toHaveCount(0); // never placed without a click or Enter

  await page.keyboard.press("Escape");
  await page.locator("body").click({ position: { x: 5, y: 5 } });
  await page.keyboard.press("j");
  await expect(page.getByRole("heading", { name: "TECX", level: 2 })).toBeVisible();
  await page.keyboard.press("k");
  await expect(page.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
});

test("cancel all open orders from the keyboard asks first", async ({ page, open, request, account }) => {
  const px = await lastPrice(request, "MKTX");
  await order(request, account, { side: "buy", qty: "1", type: "limit", limit_price: (px * 0.9).toFixed(2) });
  await order(request, account, { side: "buy", qty: "2", type: "limit", limit_price: (px * 0.8).toFixed(2) });
  await open();
  await chartReady(page);
  await page.keyboard.press("Shift+X");
  const d = page.getByRole("dialog", { name: "Cancel all open orders" });
  await expect(d).toContainText("This cancels 2 open orders");
  await d.getByRole("button", { name: "Keep orders" }).click();
  await expect(d).toHaveCount(0);
  const open1 = (await (await request.get(`/api/accounts/${account.id}/orders`)).json()).filter((o: any) => o.status === "working");
  expect(open1).toHaveLength(2);
  await page.keyboard.press("Shift+X");
  await d.getByRole("button", { name: "Cancel all orders" }).click();
  await expect(page.getByText("Canceled 2 open orders")).toBeVisible();
  await expect.poll(async () => (await (await request.get(`/api/accounts/${account.id}/orders`)).json()).filter((o: any) => o.status === "working").length).toBe(0);
});

test("no console errors when switching symbol, timeframe, indicators and views quickly", async ({ page, open, request, account }) => {
  const px = await lastPrice(request, "MKTX");
  await order(request, account, { side: "buy", qty: "5", type: "market", take_profit: (px * 1.05).toFixed(2), stop_loss: (px * 0.95).toFixed(2) });
  await order(request, account, { side: "buy", qty: "2", type: "limit", limit_price: (px * 0.97).toFixed(2) });
  await order(request, account, { side: "sell", qty: "1", type: "stop", stop_price: (px * 0.96).toFixed(2) });
  await open();
  await chartReady(page);
  await page.getByRole("button", { name: /^Indicators/ }).click();
  const panel = page.getByRole("dialog", { name: "Indicators" });
  for (const t of ["rsi", "macd", "vwap"]) await panel.getByLabel("Add indicator").selectOption(t);
  await panel.getByLabel("Close indicators").click();
  const watch = page.getByRole("region", { name: "Watchlist" });
  const tfs = page.getByRole("group", { name: "Timeframe" });
  for (let round = 0; round < 3; round++) {
    for (const s of ["TECX", "MKTX", "NVLX", "MKTX"]) await watch.getByRole("cell", { name: s, exact: true }).click();
    for (const tf of ["1M", "1D", "15M", "1W", "5M"]) await tfs.getByRole("button", { name: tf, exact: true }).click();
    await page.getByLabel("Chart type").selectOption(round % 2 ? "line" : "candles");
    await page.getByRole("tab", { name: "Options" }).click();
    await page.getByRole("tab", { name: "Chart" }).click();
    await page.getByRole("button", { name: /^Indicators/ }).click();
    await panel.getByLabel("RSI period").fill(String(10 + round));
    await panel.getByLabel("Close indicators").click();
    await page.getByRole("tab", { name: "Company" }).click();
    await page.getByRole("tab", { name: "Chart" }).click();
    await page.keyboard.press("j");
    await page.keyboard.press("k");
  }
  await chartReady(page);
  await expect(page.getByRole("button", { name: /^Buy 2 limit at/ })).toBeVisible();
  await page.waitForTimeout(1500); // let streamed ticks land on the rebuilt chart
});

test.describe("replay", () => {
  // Replay mode is global on the server; never leave it running for the next test.
  test.afterEach(async ({ request }) => { await request.post("/api/replay/exit").catch(() => {}); });

  test("a line drawn in a replay stays through clock steps and does not touch the live chart", async ({ page, open }) => {
    await open();
    await chartReady(page);
    const draw = page.getByRole("button", { name: "Draw", exact: true });
    const drawingsBtn = () => page.getByRole("button", { name: /^Drawings \(/ });
    const list = page.getByRole("dialog", { name: "Drawings" });
    const addHLine = async (fy: number) => {
      await page.getByRole("button", { name: "Horizontal line" }).click();
      const box = await stableBox(page.getByTestId("draw-layer"));
      await page.mouse.click(box.x + box.width / 2, box.y + box.height * fy);
      await drawingsBtn().click();
      const text = (await list.getByRole("button", { name: /^Horizontal line at / }).last().textContent())!;
      await drawingsBtn().click();
      return text;
    };
    // A live drawing first.
    await draw.click();
    const liveLine = await addHLine(0.4);
    await page.getByRole("button", { name: "Done" }).click();

    // Start a replay: the chart remounts on the recorded session's prices, with its own (empty) drawings.
    await page.getByRole("button", { name: "Replay", exact: true }).click();
    await page.getByRole("dialog", { name: "Replay a trading session" }).getByRole("button", { name: /^Start with/ }).click();
    await expect(page.getByRole("note")).toContainText("Replay of");
    await chartReady(page);
    await draw.click();
    await expect(drawingsBtn()).toHaveText("Drawings (0)");
    const replayLine = await addHLine(0.6);
    await page.getByRole("button", { name: "Done" }).click();

    // Step the simulated clock twice: the chart reloads, the line is still there at the same price.
    const bar = page.getByRole("region", { name: "Replay controls" });
    for (let i = 0; i < 2; i++) {
      await page.waitForTimeout(500); // let the debounced save go out before the remount
      await bar.getByRole("button", { name: "+1 min" }).click();
      await chartReady(page);
    }
    await draw.click();
    await drawingsBtn().click();
    await expect(list.getByRole("button", { name: replayLine, exact: true })).toBeVisible();
    await expect(list.getByRole("button", { name: /^Horizontal line at / })).toHaveCount(1);
    await drawingsBtn().click();
    await page.getByRole("button", { name: "Done" }).click();

    // Exit: the live chart has its own line only.
    await bar.getByRole("button", { name: "Exit replay" }).click();
    await confirmDialog(page, "Exit replay");
    await expect(page.getByRole("note")).toContainText("Paper trading · no real money");
    await chartReady(page);
    await draw.click();
    await drawingsBtn().click();
    await expect(list.getByRole("button", { name: liveLine, exact: true })).toBeVisible();
    await expect(list.getByRole("button", { name: /^Horizontal line at / })).toHaveCount(1);
  });
});
