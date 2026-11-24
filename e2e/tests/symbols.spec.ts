import { test, expect } from "./fixtures";

// A browser remembers the last symbol in localStorage. After a switch between the simulated and the live feed
// that symbol may not exist on the active feed (a fictional symbol on real data, or a real one on simulated
// data). The app must move to a valid symbol before requesting anything for it: the `problems` fixture fails
// the test on any console error, which a 404 for /api/bars/<unknown> would cause.
test("a remembered symbol the active feed does not know falls back to the watchlist without errors", async ({ page, account }) => {
  await page.addInitScript(([a]) => {
    localStorage.setItem("pd.account", a);
    localStorage.setItem("pd.symbol", "AAPL"); // a real symbol; the simulated feed only has fictional ones
  }, [account.id]);
  await page.goto("/");
  const first = (await (await page.request.get("/api/watchlists")).json()).Main[0];
  await expect(page.getByRole("heading", { name: first, level: 2 })).toBeVisible();
  await expect(page.getByRole("img", { name: `${first} price chart` })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("pd.symbol"))).toBe(first);
});

test("the symbol list is bounded and search still finds every symbol", async ({ request }) => {
  const all = await (await request.get("/api/symbols")).json();
  expect(all.length).toBeLessThanOrEqual(50);
  const found = await (await request.get("/api/symbols?q=strm")).json();
  expect(found[0].symbol).toBe("STRM");
  const look = await (await request.get("/api/symbols/lookup?symbols=AAPL,MKTX")).json();
  expect(look.map((r: any) => r.symbol)).toEqual(["MKTX"]);
});
