import { expect, type APIRequestContext } from "@playwright/test";

// Three 5-share MKTX lots (L1, L2, L3 in a fresh account), bought through the API.
export async function threeLots(request: APIRequestContext, accountId: string) {
  for (let i = 0; i < 3; i++) {
    const r = await request.post(`/api/accounts/${accountId}/orders`, { data: { symbol: "MKTX", side: "buy", qty: 5 } });
    expect(r.ok()).toBeTruthy();
  }
}
