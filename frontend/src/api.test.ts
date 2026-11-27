import { afterEach, describe, expect, it, vi } from "vitest";
import { api, BUSY_RETRIES } from "./util";

const reply = (status: number, body: any, headers: Record<string, string> = {}) => ({
  status, ok: status >= 200 && status < 300, json: async () => body,
  headers: { get: (k: string) => headers[k] ?? null },
});
const BUSY = { detail: "The simulator is busy. Try again in a moment.", busy: true };

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("api() and a busy server (HTTP 503)", () => {
  it("waits as asked and sends the same request again, so an order is placed once", async () => {
    vi.useFakeTimers();
    const calls: any[] = [];
    const answers = [reply(503, BUSY, { "Retry-After": "1" }), reply(200, [{ id: "O1" }])];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: any) => { calls.push([url, init]); return answers.shift(); }));
    const order = { symbol: "MKTX", side: "buy", qty: "1", client_id: "ui-1" };
    const done = api("/accounts/A1/orders", { method: "POST", body: order });
    await vi.advanceTimersByTimeAsync(999);
    expect(calls).toHaveLength(1); // not before the server's Retry-After
    await vi.advanceTimersByTimeAsync(1);
    expect(await done).toEqual([{ id: "O1" }]);
    expect(calls).toHaveLength(2);
    expect(calls[1][1].body).toBe(calls[0][1].body); // same client_id: the server keeps it to one order
    expect(JSON.parse(calls[1][1].body).client_id).toBe("ui-1");
  });

  it("gives up after a few tries with the plain busy message", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn(async () => reply(503, BUSY, { "Retry-After": "1" }));
    vi.stubGlobal("fetch", fetch);
    const done = api("/accounts/A1").catch((e) => e);
    await vi.advanceTimersByTimeAsync(10_000);
    const err = await done;
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe("The simulator is busy. Try again in a moment.");
    expect(fetch).toHaveBeenCalledTimes(BUSY_RETRIES + 1);
  });

  it("does not retry other server errors", async () => {
    const fetch = vi.fn(async () => reply(503, { detail: "Market data is unavailable: the provider did not answer." }));
    vi.stubGlobal("fetch", fetch);
    await expect(api("/quotes?symbols=MKTX")).rejects.toThrow("Market data is unavailable: the provider did not answer.");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
