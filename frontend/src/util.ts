export async function api(path: string, opts: { method?: string; body?: any } = {}): Promise<any> {
  const r = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const j = await r.json().catch(() => null);
  if (r.status === 401 && j?.auth_required) signedOut();
  if (!r.ok) throw new Error((j && j.detail) || r.statusText || "Request failed");
  return j;
}

// The server needs a passcode and this browser's session is missing or has expired. Login.tsx listens for
// this and shows the sign-in screen in place of the app.
export const SIGNED_OUT = "pd:signed-out";
export const signedOut = () => window.dispatchEvent(new Event(SIGNED_OUT));

// True unless the server says this browser must sign in. Network errors count as "still signed in" so the
// caller keeps its normal reconnect behaviour.
export async function sessionOk(): Promise<boolean> {
  try {
    const r = await fetch("/api/auth/state");
    if (!r.ok) return true;
    const s = await r.json();
    return !s.auth_required || s.authenticated;
  } catch { return true; }
}

const isNum = (v: any): v is number => typeof v === "number" && isFinite(v);

export const money = (v: any, dp = 2) =>
  isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "–";

export const usd = (v: any) => (isNum(v) ? (v < 0 ? "-$" : "$") + money(Math.abs(v)) : "–");

export const signed = (v: any, dp = 2) => (isNum(v) ? (v > 0 ? "+" : "") + money(v, dp) : "–");

export const pct = (v: any) => (isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(2) + "%" : "–");

export const qty = (v: any) => (isNum(v) ? v.toLocaleString("en-US", { maximumFractionDigits: 4 }) : "–");

export function compact(v: any): string {
  if (!isNum(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

export const tone = (v: any) => (isNum(v) && v !== 0 ? (v > 0 ? "up" : "down") : "");

export const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

export const dateTime = (ts: number) =>
  new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export const isOption = (s: string) => /^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(s);

export const underlyingOf = (s: string) => (isOption(s) ? s.replace(/\d{6}[CP]\d{8}$/, "") : s);

export const ORDER_TYPES: Record<string, string> = {
  market: "Market", limit: "Limit", stop: "Stop", stop_limit: "Stop limit", trailing_stop: "Trailing stop",
};
export const TIFS: Record<string, string> = { day: "Day", gtc: "Good till canceled", ioc: "Immediate or cancel", fok: "Fill or kill" };
export const STATUS: Record<string, string> = {
  held: "Held", working: "Working", partially_filled: "Partly filled", filled: "Filled", canceled: "Canceled",
  rejected: "Rejected", expired: "Expired",
};

export function store(key: string, value?: string): string | null {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch {}
  return null;
}
