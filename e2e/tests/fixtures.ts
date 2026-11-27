import { test as base, expect, type APIRequestContext, type Page } from "@playwright/test";

export type Account = { id: string; name: string };

export async function createAccount(request: APIRequestContext, type = "margin", cash = "100000"): Promise<Account> {
  const name = `E2E ${type} ${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`;
  const r = await request.post("/api/accounts", { data: { name, type, starting_cash: cash } });
  expect(r.ok()).toBeTruthy();
  return { id: (await r.json()).id, name };
}

export async function lastPrice(request: APIRequestContext, symbol: string): Promise<number> {
  const r = await request.get(`/api/quotes?symbols=${symbol}`);
  return (await r.json())[symbol].last;
}

type Fixtures = {
  problems: string[];
  account: Account;
  open: (account?: Account, symbol?: string) => Promise<void>;
};

// Browser messages that are not errors in the app. WebKit logs one at error level for the viewport meta key
// interactive-widget, which index.html sets on purpose for Chrome and Firefox on Android (the layout shrinks
// when the on-screen keyboard opens) and which Safari ignores.
const BENIGN = [/^Viewport argument key "interactive-widget" not recognized and ignored\.$/];

// Every test fails if the page logs a console error or throws. A fresh account is created for each
// test, so tests do not depend on each other's orders or positions.
export const test = base.extend<Fixtures>({
  problems: [async ({ page }, use) => {
    const problems: string[] = [];
    page.on("console", (m) => { if (m.type() === "error" && !BENIGN.some((b) => b.test(m.text()))) problems.push(`console: ${m.text()}`); });
    page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
    await use(problems);
    expect(problems, "console errors or page errors").toEqual([]);
  }, { auto: true }],
  account: async ({ request }, use) => { await use(await createAccount(request)); },
  open: async ({ page, account }, use) => {
    await use(async (acc = account, symbol = "MKTX") => {
      await page.addInitScript(([a, s, token]) => {
        localStorage.setItem("pd.account", a);
        localStorage.setItem("pd.symbol", s);
        // The first-run welcome card has its own test (ux.spec.ts); it is dismissed for every other test.
        if (localStorage.getItem("pd.welcomed") === null) localStorage.setItem("pd.welcomed", "1");
        // The phone layout remembers its last view; each open() starts on the Chart view, as documented, while a
        // reload later in the test keeps what the test chose (only the first load after this open() clears it).
        if (sessionStorage.getItem("e2e.opened") !== token) {
          localStorage.removeItem("pd.screen");
          sessionStorage.setItem("e2e.opened", token);
        }
      }, [acc.id, symbol, `${Date.now()}-${Math.random()}`]);
      await page.goto("/");
      // The phone layout keeps the account picker in its menu, so check the account on the root element there.
      if ((page.viewportSize()?.width ?? 1440) < 900) await expect(page.locator(".m-shell")).toHaveAttribute("data-account", acc.id);
      else await expect(page.getByRole("combobox", { name: "Account" })).toHaveValue(acc.id);
      await expect(page.getByRole("heading", { name: symbol, level: 2 })).toBeVisible();
    });
  },
});

export { expect };

export const ticket = (page: Page) => page.getByRole("region", { name: "Order ticket" });
export const bottomTab = (page: Page, name: string | RegExp) => page.locator(".bottom").getByRole("tab", { name });

export async function review(page: Page, shares: string) {
  const t = ticket(page);
  await t.getByLabel("Shares").fill(shares);
  await t.getByRole("button", { name: /^Review/ }).click();
}

// Exiting a replay asks for confirmation first (Confirm.tsx).
export async function confirmDialog(page: Page, button: string | RegExp, tap = false) {
  const dlg = page.getByRole("alertdialog");
  await expect(dlg).toBeVisible();
  const b = dlg.getByRole("button", { name: button });
  if (tap) await b.tap(); else await b.click();
  await expect(dlg).toHaveCount(0);
}

export async function place(page: Page) {
  await ticket(page).getByRole("button", { name: "Place paper order" }).click();
}
