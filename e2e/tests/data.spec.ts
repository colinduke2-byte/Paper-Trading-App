import { test, expect, createAccount } from "./fixtures";
import { readFileSync } from "node:fs";

// Data and backups (frontend/src/DataBackups.tsx, backend/app/api/routes_data.py).

test("back up, download everything, then restore it with a dry run", async ({ page, open, request, account }) => {
  const order = await request.post(`/api/accounts/${account.id}/orders`, { data: { symbol: "MKTX", side: "buy", qty: 3 } });
  expect(order.ok()).toBeTruthy();
  await open();
  await page.getByRole("button", { name: "Data", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Data and backups" });
  await expect(dialog.getByTestId("save-status")).toContainText("Saving works");
  await dialog.getByRole("button", { name: "Back up now" }).click();
  await expect(page.getByText(/Backup saved/)).toBeVisible();
  await expect(dialog.getByTestId("backup-list").getByRole("listitem").first()).toContainText("Manual");

  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByTestId("download-all").click()]);
  expect(download.suggestedFilename()).toMatch(/^paperdesk-backup-\d{8}T\d{6}Z\.zip$/);
  const zip = readFileSync(await download.path());
  expect(zip.subarray(0, 2).toString()).toBe("PK");

  // Something changes after the backup: a new account appears.
  const later = await createAccount(request, "cash", "500");
  // Restoring the downloaded file shows what would change first, and changes nothing until confirmed.
  await dialog.getByTestId("restore-file").setInputFiles({ name: "backup.zip", mimeType: "application/zip", buffer: zip });
  const preview = dialog.getByRole("region", { name: "What the restore changes" });
  await expect(preview).toContainText("Nothing has changed yet");
  await expect(preview.locator("li", { hasText: later.id })).toContainText("removed");
  expect((await (await request.get("/api/accounts")).json()).some((a: any) => a.id === later.id)).toBeTruthy();
  await preview.getByTestId("restore-apply").click();
  await expect(page.getByText("Backup restored")).toBeVisible();
  const ids = (await (await request.get("/api/accounts")).json()).map((a: any) => a.id);
  expect(ids).toContain(account.id);
  expect(ids).not.toContain(later.id);
  // The state from before the restore was kept as a backup.
  await expect(dialog.getByTestId("backup-list")).toContainText("Before a restore");
  const fills = await (await request.get(`/api/accounts/${account.id}/fills`)).json();
  expect(fills.length).toBe(1);
});

test("a damaged upload is refused with the reason", async ({ page, open, problems }) => {
  await open();
  await page.getByRole("button", { name: "Data", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Data and backups" });
  await dialog.getByTestId("restore-file").setInputFiles({ name: "x.zip", mimeType: "application/zip", buffer: Buffer.from("PK\x03\x04junk") });
  await expect(dialog.getByRole("alert")).toContainText("damaged");
  // The browser logs the expected 422 answer as a failed resource load; that one entry is not a problem.
  const i = problems.findIndex((p) => p.includes("status of 422"));
  expect(i).toBeGreaterThanOrEqual(0);
  problems.splice(i, 1);
});

test("export an account and import it again as a new account", async ({ page, open, request, account }) => {
  await request.post(`/api/accounts/${account.id}/orders`, { data: { symbol: "TECX", side: "buy", qty: 2 } });
  await open();
  await page.getByRole("button", { name: "Data", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Data and backups" });
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("link", { name: "Export this account" }).click()]);
  expect(download.suggestedFilename()).toMatch(/^paperdesk-account-.*\.json$/);
  await dialog.getByTestId("import-file").setInputFiles({ name: "a.json", mimeType: "application/json", buffer: readFileSync(await download.path()) });
  const region = dialog.getByRole("region", { name: "What the import adds" });
  await expect(region).toContainText("1 positions");
  await region.getByLabel("Name").fill("Imported copy");
  await region.getByRole("button", { name: "Import" }).click();
  await expect(page.getByText(/Account imported as A\d+/)).toBeVisible();
  const accts = await (await request.get("/api/accounts")).json();
  const copy = accts.find((a: any) => a.name === "Imported copy");
  const orig = accts.find((a: any) => a.id === account.id);
  expect(copy && copy.id !== account.id).toBeTruthy();
  expect(copy.cash).toBe(orig.cash);
});

test("the recovery screen offers restore, start fresh and quit", async ({ page }) => {
  // The server here is healthy, so the recovery answers are simulated for this page only.
  let restored = false;
  await page.route("**/api/data/status", (r) => r.fulfill({ json: restored ? { recovery: null } : {
    recovery: { kind: "damaged", reason: "state.json is not valid JSON; it looks cut off (Expecting value: line 1 column 2)", copy: "/data/recovery/20261124T093000Z" } } }));
  await page.route("**/api/data/recovery", (r) => r.fulfill({ json: { recovery: {}, backups: [
    { name: "paperdesk-auto-20261124T090000Z.zip", kind: "auto", created: "2026-11-24T09:00:00Z", created_ts: 1795510800, size: 40000 }] } }));
  await page.route("**/api/data/recovery/restore", (r) => { restored = true; return r.fulfill({ json: { ok: true } }); });
  await page.goto("/");
  const card = page.getByTestId("recovery");
  await expect(card.getByRole("heading", { name: "Saved data could not be read" })).toBeVisible();
  await expect(card).toContainText("it looks cut off");
  await expect(card).toContainText("A copy of the files is kept in /data/recovery/20261124T093000Z");
  await expect(card.getByRole("button", { name: "Quit without changing anything" })).toBeVisible();
  await card.getByRole("button", { name: "Start fresh…" }).click();
  await expect(card.getByRole("button", { name: "Start fresh with new paper accounts" })).toBeVisible();
  await card.getByRole("button", { name: /Restore the latest backup/ }).click();
  await expect(page.getByRole("region", { name: "Watchlist" })).toBeVisible(); // the app takes over
});
