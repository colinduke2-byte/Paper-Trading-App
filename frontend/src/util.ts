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

// Money and quantities as typed by people: "1,000", "$25,000.50" and " 100 " become "1000", "25000.50" and
// "100". Only well-formed thousands groups are removed, so "1,5" (a decimal comma) is left for the server to
// refuse with a message that names the field.
export function numText(v: string): string {
  let s = String(v ?? "").trim().replace(/^\$\s*/, "").replace(/^(-)\$\s*/, "$1").replace(/\s+/g, "");
  if (/^-?\d{1,3}(,\d{3})+(\.\d*)?$/.test(s)) s = s.replace(/,/g, "");
  return s;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// The same text as the server's options.symbology.describe(): "MKTX 04 Dec 2026 400 Call".
export function describeOption(s: string): string {
  const m = s.match(/^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/);
  if (!m) return s;
  const strike = String(Number(m[6]) / 1000);
  return `${m[1]} ${m[4]} ${MONTHS[Number(m[3]) - 1]} 20${m[2]} ${strike} ${m[5] === "C" ? "Call" : "Put"}`;
}

// A click on an option chain quote means "trade now at about this price". The model price moves every second,
// so a limit exactly at the clicked ask or bid often rests unfilled. The ticket is seeded with a marketable
// limit instead: 2% (at least 5 cents) through the quote. It still fills at the market price, and the limit
// caps how far the price may have moved.
export function marketableLimit(side: string, quote: number): { limit: number; note: string } {
  const room = Math.max(0.05, quote * 0.02);
  const limit = side === "buy" ? Math.ceil((quote + room) * 100 - 1e-6) / 100 : Math.max(0.01, Math.floor((quote - room) * 100 + 1e-6) / 100);
  const gap = money(Math.abs(limit - quote));
  const note = side === "buy"
    ? `Limit set ${gap} above the ask (${money(quote)}) so the order fills if the price moves a little. It fills at the market price, at most the limit. Change the limit to wait for your own price.`
    : `Limit set ${gap} below the bid (${money(quote)}) so the order fills if the price moves a little. It fills at the market price, at least the limit. Change the limit to wait for your own price.`;
  return { limit, note };
}
