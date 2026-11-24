import { test, expect, ticket, review, place } from "./fixtures";
import type { Page } from "@playwright/test";

// Runs against the second server (playwright.config.ts): passcode set, loopback bypass off, so the browser
// must sign in even though it runs on the same machine. Projects: auth-desktop and auth-pixel-7.

const PASSCODE = process.env.E2E_PASSCODE!;

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
  await expect(page.getByRole("combobox", { name: "Account" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "MKTX", level: 2 })).toBeVisible();
  await expect(page.locator(".dot.ok")).toBeVisible(); // the live stream is connected
  const cookie = (await context.cookies()).find((c) => c.name === "paperdesk_session")!;
  expect(cookie.httpOnly).toBe(true);
  expect(cookie.sameSite).toBe("Strict");
  await review(page, "1");
  await expect(ticket(page).getByText("Estimated cost")).toBeVisible();
  await place(page);
  await expect(page.getByText(/Filled: buy 1 MKTX/)).toBeVisible();
  // The session survives a reload.
  await page.reload();
  await expect(page.getByRole("combobox", { name: "Account" })).toBeVisible();
});

test("signing out returns to the sign-in screen and ends the session", async ({ page }) => {
  await page.goto("/");
  await signIn(page);
  await expect(page.getByRole("combobox", { name: "Account" })).toBeVisible();
  expect((await page.request.get("/api/accounts")).status()).toBe(200);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("status")).toHaveText("You are signed out.");
  await expect(page.getByLabel("Passcode")).toBeVisible();
  expect((await page.request.get("/api/accounts")).status()).toBe(401);
  await page.reload();
  await expect(page.getByLabel("Passcode")).toBeVisible();
});
