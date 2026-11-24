// Labels for order types, durations and linked-order roles, shared by the ticket, the strategy builder
// and the Orders tab. (util.ts keeps the original short lists; these add the newer kinds.)
import { dateTime, money } from "./util";

export const TYPE_LABELS: Record<string, string> = {
  market: "Market", limit: "Limit", stop: "Stop", stop_limit: "Stop limit", trailing_stop: "Trailing stop",
  market_on_open: "Market on open", market_on_close: "Market on close", limit_on_open: "Limit on open",
  limit_on_close: "Limit on close", multileg: "Multi-leg",
};
export const BASIC_TYPES = ["market", "limit", "stop", "stop_limit", "trailing_stop"];
export const AUCTION_TYPES = ["market_on_open", "market_on_close", "limit_on_open", "limit_on_close"];
export const isAuction = (t: string) => AUCTION_TYPES.includes(t);
export const auctionWhich = (t: string) => (t.endsWith("_open") ? "open" : "close");

export const TIF_LABELS: Record<string, string> = {
  day: "Day", gtc: "Good till canceled", gtd: "Good till date", ioc: "Immediate or cancel", fok: "Fill or kill",
  opg: "At the open", cls: "At the close",
};

export const ROLE_LABELS: Record<string, string> = {
  take_profit: "Take profit", stop_loss: "Stop loss", exit: "Exit, after the entry fills", triggered: "Placed when the entry fills",
};

// How long an order works, in words: "Good till 2026-12-31", "At the close · Nov 25, 00:00:00".
export function durationText(o: any): string {
  if ((o.tif === "opg" || o.tif === "cls") && o.auction_at) return `${TIF_LABELS[o.tif]} · ${dateTime(o.auction_at)}`;
  if (o.tif === "gtd" && o.good_till) return `Good till ${o.good_till}`;
  return TIF_LABELS[o.tif] || o.tif;
}

// "1.25 debit", "0.80 credit", "even" for a signed net price per share (positive = debit).
export function netText(net: number | null | undefined): string {
  if (net == null) return "–";
  if (Math.abs(net) < 0.00005) return "0.00 even";
  return `${money(Math.abs(net))} ${net > 0 ? "debit" : "credit"}`;
}

// Local date as YYYY-MM-DD, `days` from today.
export function isoDay(days = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
