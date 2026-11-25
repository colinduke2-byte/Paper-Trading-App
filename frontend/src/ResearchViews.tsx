import { useState } from "react";
import { usePoll } from "./Market";
import { compact, dateTime, money, pct } from "./util";

// Research views that work on any data source: live providers fill what they can, the simulated hub
// (fictional companies) fills everything. Every block names its source and time.

export type Envelope = {
  source: string | null; as_of: number | null; available: boolean; stale: boolean; reason: string | null;
  simulated?: boolean; items?: any[]; data?: any;
};

/** Source line shown on every research block: provider, time of the data, and a stale or unavailable state. */
export function SourceLine({ env, error }: { env: Envelope | null; error: string }) {
  if (error) return <div className="rs-state bad" role="status">Could not load this section: {error}</div>;
  if (!env) return <div className="rs-state muted">Loading…</div>;
  if (!env.available) return <div className="rs-state" role="status">Unavailable. {env.reason}</div>;
  return (
    <div className={env.stale ? "rs-source rs-stale" : "rs-source"}>
      Source: {env.source}{env.simulated ? " (fictional companies and events)" : ""}
      {env.as_of ? ` · as of ${dateTime(env.as_of)}` : ""}
      {env.stale && <span className="rs-flag"> · stale: {env.reason}</span>}
    </div>
  );
}

/** The exchange's date (YYYY-MM-DD, New York) for an envelope, so replays compare against simulated time. */
export function dayOf(ts: number | null | undefined): string {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

const CATEGORY_LABEL: Record<string, string> = {
  earnings: "Earnings", dividend: "Dividend", split: "Split", rating: "Analyst", macro: "Economy",
  price_move: "Price move", "52_week": "52-week", sector: "Sector",
};

export function NewsList({ items, empty }: { items: any[]; empty: string }) {
  if (!items.length) return <p className="muted rs-pad">{empty}</p>;
  return (
    <ul className="rs-news">
      {items.map((n) => (
        <li key={n.id ?? n.url}>
          {n.url ? <a href={n.url} target="_blank" rel="noopener noreferrer">{n.headline}</a>
            : <span className="rs-headline">{n.headline}</span>}
          <div className="muted rs-meta">
            {n.category && <span className="rs-cat">{CATEGORY_LABEL[n.category] || n.category}</span>}
            {n.source}{n.created_at ? ` · ${dateTime(n.created_at)}` : ""}
            {n.symbols?.length ? ` · ${n.symbols.slice(0, 6).join(", ")}${n.symbols.length > 6 ? "…" : ""}` : ""}
          </div>
          {n.summary && <p className="rs-summary">{stripTags(n.summary)}</p>}
        </li>
      ))}
    </ul>
  );
}

export function stripTags(s: string) {
  return s.replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"');
}

// ---- statements ---------------------------------------------------------------------------------------
const LINES: [string, string, [string, string, "money" | "eps" | "count"][]][] = [
  ["Income statement", "income", [
    ["Revenue", "revenue", "money"], ["Cost of revenue", "cost_of_revenue", "money"], ["Gross profit", "gross_profit", "money"],
    ["Operating expenses", "operating_expenses", "money"], ["Operating income", "operating_income", "money"],
    ["Interest expense", "interest_expense", "money"], ["Pre-tax income", "pretax_income", "money"],
    ["Income tax", "income_tax", "money"], ["Net income", "net_income", "money"], ["Shares", "shares", "count"],
    ["Earnings per share", "eps", "eps"]]],
  ["Balance sheet", "balance", [
    ["Cash", "cash", "money"], ["Receivables", "receivables", "money"], ["Inventory", "inventory", "money"],
    ["Property and equipment", "property_equipment", "money"], ["Other assets", "other_assets", "money"],
    ["Total assets", "total_assets", "money"], ["Accounts payable", "accounts_payable", "money"], ["Debt", "debt", "money"],
    ["Other liabilities", "other_liabilities", "money"], ["Total liabilities", "total_liabilities", "money"],
    ["Shareholders' equity", "equity", "money"]]],
  ["Cash flow", "cash_flow", [
    ["Net income", "net_income", "money"], ["Depreciation", "depreciation", "money"], ["Working capital", "working_capital", "money"],
    ["Operating cash flow", "operating", "money"], ["Capital expenditure", "capital_expenditure", "money"],
    ["Investing cash flow", "investing", "money"], ["Dividends paid", "dividends_paid", "money"],
    ["Debt issued or repaid", "debt_issued_repaid", "money"], ["Buybacks and other", "buybacks_and_other", "money"],
    ["Financing cash flow", "financing", "money"], ["Change in cash", "net_change_in_cash", "money"],
    ["Free cash flow", "free_cash_flow", "money"]]],
];

const fmtLine = (v: any, kind: string) => kind === "eps" ? money(v) : kind === "count" ? compact(v) : (v < 0 ? "-" : "") + compact(Math.abs(v));

export function Statements({ symbol }: { symbol: string }) {
  const [env, error] = usePoll<Envelope>(`/research/statements/${symbol}`, 60000);
  const [period, setPeriod] = useState<"quarterly" | "annual">("quarterly");
  const d = env?.available ? env.data : null;
  const cols: any[] = d ? d[period] || [] : [];
  return (
    <section className="rs-block rs-wide" aria-labelledby="rs-st">
      <div className="rs-head">
        <h3 id="rs-st">Financial statements</h3>
        <div className="rs-seg" role="group" aria-label="Statement period">
          {(["quarterly", "annual"] as const).map((p) => (
            <button key={p} className={p === period ? "on" : ""} aria-pressed={p === period} onClick={() => setPeriod(p)}>
              {p === "quarterly" ? "Quarterly" : "Annual"}
            </button>
          ))}
        </div>
      </div>
      <SourceLine env={env} error={error} />
      {env?.available && env.reason && <p className="muted rs-pad">{env.reason}</p>}
      {d?.per_share_note && <p className="muted rs-pad rs-small">Amounts in US dollars. {d.per_share_note}</p>}
      {cols.length > 0 && LINES.map(([title, part, rows]) => (
        <div key={part} className="rs-scroll" role="region" aria-label={title} tabIndex={0}>
          <table className="tight rs-table">
            <thead><tr><th>{title}</th>{cols.map((c) => <th key={c.period} className="num">{c.period}</th>)}</tr></thead>
            <tbody>
              {rows.map(([label, key, kind]) => (
                <tr key={key} className={["gross_profit", "operating_income", "net_income", "total_assets", "equity", "operating", "net_change_in_cash"].includes(key) ? "rs-total" : ""}>
                  <td>{label}</td>
                  {cols.map((c) => <td key={c.period} className={"num" + (c[part][key] < 0 ? " down" : "")}>{fmtLine(c[part][key], kind)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

// ---- key ratios and fund holdings (Company tab and Research) --------------------------------------------
const pctOrDash = (v: any) => (v == null ? "–" : v.toFixed(1) + "%");
const num2 = (v: any) => (v == null ? "–" : v.toFixed(2));

export function KeyRatios({ f }: { f: any }) {
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  if (f.kind === "fund") {
    return (
      <>
        <div className="rs-facts">
          {row("Assets", compact(f.aum))}
          {row("Holdings", String(f.holdings_count))}
          {row("Expense ratio", f.expense_ratio_pct == null ? "–" : f.expense_ratio_pct.toFixed(2) + "%")}
          {row("P/E of holdings", num2(f.pe))}
          {row("Dividend yield", pctOrDash(f.dividend_yield_pct))}
          {row("Beta", num2(f.beta))}
        </div>
        <h4 className="rs-sub">Sector weights</h4>
        <ul className="rs-bars">
          {f.sector_weights.map((s: any) => (
            <li key={s.sector}><span>{s.sector}</span><i style={{ width: `${s.weight_pct}%` }} aria-hidden="true" /><b>{s.weight_pct.toFixed(1)}%</b></li>
          ))}
        </ul>
        <h4 className="rs-sub">Top holdings</h4>
        <ul className="rs-bars">
          {f.holdings.slice(0, 10).map((h: any) => (
            <li key={h.symbol}><span><b>{h.symbol}</b> {h.name}</span><i style={{ width: `${h.weight_pct}%` }} aria-hidden="true" /><b>{h.weight_pct.toFixed(1)}%</b></li>
          ))}
        </ul>
      </>
    );
  }
  const r = f.analyst_rating;
  return (
    <div className="rs-facts">
      {row("P/E (trailing)", num2(f.pe))}
      {row("Price / sales", num2(f.ps))}
      {row("Price / book", num2(f.pb))}
      {row("EPS (trailing)", money(f.eps))}
      {row("Revenue (trailing)", compact(f.revenue_ttm))}
      {row("Revenue growth", pctOrDash(f.revenue_growth_pct))}
      {row("Earnings growth", pctOrDash(f.eps_growth_pct))}
      {row("Gross margin", pctOrDash(f.gross_margin_pct))}
      {row("Operating margin", pctOrDash(f.operating_margin_pct))}
      {row("Net margin", pctOrDash(f.net_margin_pct))}
      {row("Return on equity", pctOrDash(f.roe_pct))}
      {row("Debt / equity", num2(f.debt_to_equity))}
      {row("Payout ratio", pctOrDash(f.payout_ratio_pct))}
      {row("Dividend yield", pctOrDash(f.dividend_yield_pct))}
      {row("Free cash flow (trailing)", compact(f.free_cash_flow_ttm))}
      {row("Beta", num2(f.beta))}
      {f.next_earnings && row("Next earnings", `${f.next_earnings.date} ${f.next_earnings.hour}`)}
      {r && row("Analyst rating", `${r.rating}${r.target ? ` · target ${money(r.target)}` : ""}`)}
    </div>
  );
}

// ---- market-wide: news, economic calendar, earnings calendar ----------------------------------------------
const CATEGORIES = ["", "earnings", "macro", "rating", "price_move", "sector", "52_week", "dividend", "split"];

export function MarketNews() {
  const [cat, setCat] = useState("");
  const [env, error] = usePoll<Envelope>(`/research/news?limit=40${cat ? `&category=${cat}` : ""}`, 60000, [cat]);
  return (
    <section className="rs-block" aria-labelledby="rs-mnews">
      <div className="rs-head">
        <h3 id="rs-mnews">Market news</h3>
        <label className="inline">Show
          <select value={cat} onChange={(e) => setCat(e.target.value)} aria-label="News category">
            {CATEGORIES.map((c) => <option key={c} value={c}>{c ? CATEGORY_LABEL[c] : "All"}</option>)}
          </select>
        </label>
      </div>
      <SourceLine env={env} error={error} />
      {env?.available && <NewsList items={env.items || []} empty="No market news in the last ten days." />}
    </section>
  );
}

export function EconomicCalendar() {
  const [env, error] = usePoll<Envelope>("/research/economic-calendar", 60000);
  const items = env?.available ? env.items || [] : [];
  const fmt = (r: any, v: any) => (v == null ? "–" : r.kind === "jobs" ? `${v.toLocaleString("en-US")}K` : `${v}%`);
  return (
    <section className="rs-block" aria-labelledby="rs-econ">
      <h3 id="rs-econ">Economic calendar</h3>
      <SourceLine env={env} error={error} />
      {env?.available && !items.length && <p className="muted rs-pad">No releases in this window.</p>}
      <ul className="rs-list rs-econ">
        {items.map((r) => (
          <li key={r.id} className={r.released ? "" : "rs-upcoming"}>
            <b>{r.date} {r.time}</b>
            <span>{r.name}{r.period ? ` (${r.period})` : ""}</span>
            <span className="muted">
              {r.released ? `actual ${fmt(r, r.actual)} · ` : ""}forecast {fmt(r, r.forecast)}
              {r.previous != null ? ` · previous ${fmt(r, r.previous)}` : ""}
              {r.unemployment_rate != null ? ` · unemployment ${r.unemployment_rate}%` : ""}
            </span>
            {r.market_move_pct != null && <span className={r.market_move_pct >= 0 ? "up" : "down"}>market move {pct(r.market_move_pct)}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function EarningsCalendar({ held, onSelect }: { held: string[]; onSelect?: (s: string) => void }) {
  const [env, error] = usePoll<Envelope>("/research/earnings-calendar", 60000);
  const items = env?.available ? env.items || [] : [];
  const today = dayOf(env?.as_of);
  const asOf = env?.as_of ?? 0;
  // Reports within two weeks for symbols the account holds (shares or options on them).
  const soon = items.filter((r) => r.status === "upcoming" && held.includes(r.symbol) && r.ts - asOf <= 14 * 86400);
  return (
    <section className="rs-block" aria-labelledby="rs-earn">
      <h3 id="rs-earn">Earnings calendar</h3>
      <SourceLine env={env} error={error} />
      {soon.length > 0 && (
        <div className="notice rs-alert" role="status">
          Earnings coming up for symbols you hold: {soon.map((r) => `${r.symbol} on ${r.date} (${r.hour})`).join(", ")}.
          Prices can gap on the report, and stop orders then fill at the gap price.
        </div>
      )}
      {env?.available && !items.length && <p className="muted rs-pad">No reports in this window.</p>}
      <ul className="rs-list">
        {items.map((r) => (
          <li key={`${r.symbol}-${r.quarter_index ?? r.date}`} className={r.date >= today ? "rs-upcoming" : ""}>
            <b>{r.date}</b>
            {onSelect ? <button className="linkish" onClick={() => onSelect(r.symbol)}>{r.symbol}</button> : <span>{r.symbol}</span>}
            {held.includes(r.symbol) && <span className="tag">held</span>}
            <span className="muted">{r.hour} · Q{r.quarter} {r.year}</span>
            <span className="muted">EPS est. {money(r.eps_estimate)}
              {r.eps_actual != null ? ` · actual ${money(r.eps_actual)} (${r.result})` : r.expected_move_pct != null ? ` · implied move ±${r.expected_move_pct}%` : ""}
              {r.price_reaction_pct != null ? ` · gap ${pct(r.price_reaction_pct)}` : ""}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
