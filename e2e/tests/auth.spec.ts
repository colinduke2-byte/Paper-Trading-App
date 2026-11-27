import { test, expect, ticket, review, place } from "./fixtures";
import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";

// Runs against the second server (playwright.config.ts): passcode set, loopback bypass off, so the browser
// must sign in even though it runs on the same machine. Projects: auth-desktop and auth-pixel-7.

const PASSCODE = process.env.E2E_PASSCODE!;

// The auth-pixel-7 project renders the phone shell: the account picker and the actions live in its menu.
const phone = (page: Page) => page.viewportSize()!.width < 900;
const signedIn = (page: Page) => phone(page)
  ? page.getByRole("navigation", { name: "Views" })
  : page.getByRole("combobox", { name: "Account" });
const view = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "Views" }).getByRole("button", { name, exact: true }).tap();

async function signIn(page: Page, passcode = PASSCODE) {
  await page.getByLabel("Passcode").fill(passcode);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("the sign-in screen is shown and no data is reachable before signing in", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "PaperDesk", level: 1 })).toBeVisible();
  const input = page.getByLabel("Passcode");
  await expect(input).toBeVisible();
  await expect(input).toHaveAttribute("type", "password");
  await expect(page.getByRole("combobox", { name: "Account" })).toHaveCount(0);
  // Phone friendly: 16 px text (no zoom on focus) and 40 px+ targets.
  const size = await input.evaluate((el) => ({ font: getComputedStyle(el).fontSize, h: el.getBoundingClientRect().height }));
  expect(size.font).toBe("16px");
  expect(size.h).toBeGreaterThanOrEqual(40);
  expect((await page.getByRole("button", { name: "Sign in" }).boundingBox())!.height).toBeGreaterThanOrEqual(40);
  expect((await page.request.get("/api/accounts")).status()).toBe(401);
});

test("a wrong passcode shows a message", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "not the passcode");
  await expect(page.getByRole("alert")).toHaveText("That passcode is not correct.");
  await expect(page.getByLabel("Passcode")).toHaveValue("");
  await expect(page.getByRole("combobox", { name: "Account" })).toHaveCount(0);
});

test("after signing in, trading works", async ({ page, context }) => {
  await page.goto("/");
  await signIn(page);
  await expect(signedIn(page)).toBeVisible();
  await expect(page.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
  await expect(page.locator(".dot.ok").first()).toBeVisible(); // the live stream is connected
  const cookie = (await context.cookies()).find((c) => c.name === "paperdesk_session")!;
  expect(cookie.httpOnly).toBe(true);
  expect(cookie.sameSite).toBe("Strict");
  if (phone(page)) await view(page, "Trade");
  await review(page, "1");
  await expect(ticket(page).getByText("Estimated cost")).toBeVisible();
  await place(page);
  await expect(page.getByText(/Filled: buy 1 MKTX/)).toBeVisible();
  // The session survives a reload.
  await page.reload();
  await expect(signedIn(page)).toBeVisible();
});

test("signing out returns to the sign-in screen and ends the session", async ({ page }) => {
  await page.goto("/");
  await signIn(page);
  await expect(signedIn(page)).toBeVisible();
  expect((await page.request.get("/api/accounts")).status()).toBe(200);
  if (phone(page)) {  // Sign out is an entry in the phone menu
    await page.getByRole("button", { name: "Menu" }).tap();
    await page.getByRole("dialog", { name: "Menu" }).getByRole("button", { name: "Sign out" }).tap();
  } else {
    await page.getByRole("button", { name: "Sign out" }).click();
  }
  await expect(page.getByRole("status")).toHaveText("You are signed out.");
  await expect(page.getByLabel("Passcode")).toBeVisible();
  expect((await page.request.get("/api/accounts")).status()).toBe(401);
  await page.reload();
  await expect(page.getByLabel("Passcode")).toBeVisible();
});

test("signed in, CSV export downloads and the printable statement prints with the CSP on", async ({ page, context }) => {
  // The CSP (script-src 'self') applies to every response; a violation is a console error, which fails
  // the test through the fixture. The statement opens in a new tab, which gets its own listener.
  await page.goto("/");
  await signIn(page);
  await expect(signedIn(page)).toBeVisible();
  if (phone(page)) await view(page, "Account");
  await page.locator(".bottom").getByRole("tab", { name: "Reports" }).click();
  await page.locator(".bottom").getByRole("button", { name: "Export", exact: true }).click();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator(".bottom").getByRole("link", { name: /^Fills \(CSV\)/ }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.csv$/);
  const text = readFileSync(await download.path(), "utf8");
  expect(text.split("\n")[0]).toContain("symbol");

  await page.locator(".bottom").getByRole("button", { name: "Statements", exact: true }).click();
  await context.addInitScript(() => { (window as any).__printed = 0; window.print = () => { (window as any).__printed += 1; }; });
  const problems: string[] = [];
  const [statement] = await Promise.all([
    context.waitForEvent("page"),
    page.locator(".bottom").getByRole("link", { name: "Open printable statement" }).click(),
  ]);
  statement.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
  await statement.waitForLoadState("domcontentloaded"); // not "load": see navDefaults in fixtures.ts
  expect(statement.url()).toContain("/statements/");
  await statement.getByRole("button", { name: "Print" }).click();
  await expect.poll(() => statement.evaluate(() => (window as any).__printed)).toBe(1);
  expect(problems, "console errors on the statement page (CSP)").toEqual([]);
  await statement.close();
});
