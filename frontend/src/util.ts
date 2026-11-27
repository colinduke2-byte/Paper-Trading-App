export async function api(path: string, opts: { method?: string; body?: any } = {}): Promise<any> {
  let r: Response;
  try {
    r = await fetch("/api" + path, {
      method: opts.method || "GET",
      headers: opts.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw new Error("Cannot reach the PaperDesk server. Check that it is still running.");
  }
  const j = await r.json().catch(() => null);
  if (r.status === 401 && j?.auth_required) signedOut();
  if (!r.ok) throw new Error(errorText(r.status, j?.detail));
  return j;
}

// Field names in validation errors, as the user sees them.
const FIELD_LABELS: Record<string, string> = {
  min_price: "Min price", max_price: "Max price", min_change_pct: "Min change %", max_pe: "Max P/E",
  min_dividend_yield: "Min yield %", limit: "Limit", qty: "Quantity", price: "Price", starting_cash: "Starting cash",
  risk_free: "Risk-free rate", benchmark: "Benchmark",
};

/** A readable message for an error response: FastAPI validation lists, plain details and server faults. */
export function errorText(status: number, detail: any): string {
  if (Array.isArray(detail)) {
    const msgs = detail.map((d: any) => {
      const key = Array.isArray(d?.loc) ? String(d.loc[d.loc.length - 1]) : "";
      const label = FIELD_LABELS[key] || (key ? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "A value");
      const t = String(d?.type || "");
      if (/float|int|decimal|number/.test(t)) return `${label} must be a number.`;
      if (t === "missing") return `${label} is required.`;
      return `${label}: ${String(d?.msg || "not valid").replace(/^./, (c) => c.toLowerCase())}.`;
    });
    return [...new Set(msgs)].join(" ") || "The request was not valid.";
  }
  if (typeof detail === "string" && detail) return detail;
  if (status >= 500) return "Something went wrong on the server. The details are in the window where PaperDesk is running.";
  if (status === 404) return "Not found.";
  return "The request failed.";
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
// Rounds to the shown precision first, so -0.001 prints as 0.00 rather than -0.00.
const clean = (v: number, dp: number) => { const r = Number(v.toFixed(dp)); return r === 0 ? 0 : r; };
const MINUS = "\u2212"; // a real minus sign, easier to read than a hyphen next to digits

export const money = (v: any, dp = 2) =>
  isNum(v) ? clean(v, dp).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "–";

/** $1,234.56 or −$1,234.56. */
export const usd = (v: any, dp = 2) => {
  if (!isNum(v)) return "–";
  const c = clean(v, dp);
  return (c < 0 ? MINUS + "$" : "$") + money(Math.abs(c), dp);
};


/** +1,234.56 / −1,234.56 / 0.00 (no currency). */
export const signed = (v: any, dp = 2) => {
  if (!isNum(v)) return "–";
  const c = clean(v, dp);
  return (c > 0 ? "+" : c < 0 ? MINUS : "") + money(Math.abs(c), dp);
};

/** +$1,234.56 / −$1,234.56 / $0.00. */
export const signedUsd = (v: any, dp = 2) => {
  if (!isNum(v)) return "–";
  const c = clean(v, dp);
  return (c > 0 ? "+" : c < 0 ? MINUS : "") + "$" + money(Math.abs(c), dp);
};

/** +1.23% / −1.23% / 0.00% for a value already in percent. */
export const pct = (v: any, dp = 2) => {
  if (!isNum(v)) return "–";
  const c = clean(v, dp);
  return (c > 0 ? "+" : c < 0 ? MINUS : "") + Math.abs(c).toFixed(dp) + "%";
};

/** A plain fixed-point number without negative zero (for ratios such as delta or beta). */
export const fixed = (v: any, dp = 2) => {
  if (!isNum(v)) return "–";
  const c = clean(v, dp);
  return (c < 0 ? MINUS : "") + Math.abs(c).toFixed(dp);
};

// Up to 9 decimals: stocks have at most 4, crypto pairs use their own quantity increment.
export const qty = (v: any) => (isNum(v) ? v.toLocaleString("en-US", { maximumFractionDigits: 9 }) : "–");

/** A price with its currency and as many decimals as it needs: $454.36 from $1 up, $0.0125 down to a cent,
 *  then up to 8 decimals for small crypto prices ($0.00001234). */
export const price = (v: any) => {
  if (!isNum(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1 || a === 0) return usd(v, 2);
  if (a >= 0.01) return usd(v, 4);
  return (v < 0 ? MINUS : "") + "$" + a.toLocaleString("en-US", { minimumFractionDigits: 6, maximumFractionDigits: 8 });
};

export function compact(v: any): string {
  if (!isNum(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}

export const tone = (v: any) => { if (!isNum(v)) return ""; const c = clean(v, 2); return c > 0 ? "up" : c < 0 ? "down" : ""; };

// ---- time zones --------------------------------------------------------------------------------
// One rule: times are shown in this device's time zone and labelled with its short name, except during a
// replay, where every time is New York time (ET), the clock of the recorded session. App sets the mode.
export const NY = "America/New_York";
let zoneOverride: string | undefined = undefined; // undefined = this device's zone

export function setReplayTime(replay: boolean) { zoneOverride = replay ? NY : undefined; }
export const inReplayTime = () => zoneOverride === NY;
/** The IANA zone times are shown in, or undefined for this device's zone (for Intl formatting options). */
export const displayZone = () => zoneOverride;
const deviceZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return ""; } };
const shortName = (zone: string | undefined, at = new Date()) => {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "short" }).formatToParts(at).find((p) => p.type === "timeZoneName");
    return part?.value || "";
  } catch { return ""; }
};

/** Short label for the zone times are shown in: "ET" in replay or on a New York device, else e.g. "UTC" or "GMT+1". */
export function zoneLabel(): string {
  if (zoneOverride === NY || deviceZone() === NY) return "ET";
  return shortName(undefined) || "your time";
}

/** "Time (UTC)" style column heading. */
export const timeHeading = (word = "Time") => `${word} (${zoneLabel()})`;

/** Seconds to add to a UTC timestamp so a chart that draws UTC shows the display zone's wall clock. */
export function displayShift(at = Date.now()): number {
  if (zoneOverride !== NY) return -new Date(at).getTimezoneOffset() * 60;
  const d = new Date(at);
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: NY, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(d).reduce((o: any, p) => { o[p.type] = p.value; return o; }, {});
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour % 24, +parts.minute, +parts.second);
  return Math.round((asUtc - Math.floor(at / 1000) * 1000) / 1000);
}

export const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false, timeZone: zoneOverride });

export const dateTime = (ts: number) =>
  new Date(ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: zoneOverride });

/** New York wall-clock time, for the US session label shown next to a local clock. */
export const nyTime = (ts: number) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: NY });

/** "Thu 26 Nov 03:40" in the display zone. */
export const shortDateTime = (ts: number) =>
  new Date(ts * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: zoneOverride }).replace(",", "");

export const isOption = (s: string) => /^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(s);

// Crypto pairs are written BASE/USD (see backend/app/core/assets.py).
export const isCrypto = (s: string) => /^[A-Z0-9]{1,15}\/USD$/.test(s || "");

export const ASSET_LABELS: Record<string, string> = { stock: "Stock", option: "Option", crypto: "Crypto" };

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

// ---- cross-component hooks ---------------------------------------------------------------------
// Opens the Account rules dialog (App listens). The order ticket links rejections to the rule with it.
export const OPEN_SETTINGS = "pd:open-settings";
export const openSettings = () => window.dispatchEvent(new Event(OPEN_SETTINGS));

// Switches the bottom panel to a tab (App and Panels listen), e.g. "Orders" after placing a waiting order.
export const SHOW_TAB = "pd:show-tab";
export const showTab = (tab: string) => window.dispatchEvent(new CustomEvent(SHOW_TAB, { detail: tab }));

/** Brings the order ticket into view and focuses its quantity field (marked data-ticket-qty, else the first input). */
export function ticketFocus() {
  setTimeout(() => {
    const t = document.querySelector(".ticket") as HTMLElement | null;
    if (!t) return;
    t.scrollIntoView({ block: "nearest" });
    const q = (t.querySelector("input[data-ticket-qty]") || t.querySelector("input:not([type=checkbox])")) as HTMLInputElement | null;
    q?.focus({ preventScroll: true });
  }, 50);
}
