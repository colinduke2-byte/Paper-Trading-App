import { useState } from "react";
import { usePoll } from "./Market";
import { compact, money } from "./util";
import { dayOf, EarningsCalendar, EconomicCalendar, Envelope, KeyRatios, MarketNews, NewsList, SourceLine, Statements } from "./ResearchViews";
import "./Research.css";

function News({ symbol }: { symbol: string }) {
  const [env, error] = usePoll<Envelope>(`/research/news/${symbol}?limit=20`, 120000);
  return (
    <section className="rs-block" aria-labelledby="rs-news">
      <h3 id="rs-news">News</h3>
      <SourceLine env={env} error={error} />
      {env?.available && <NewsList items={env.items || []} empty={`No recent articles for ${symbol}.`} />}
    </section>
  );
}

function Calendar({ symbol }: { symbol: string }) {
  const [cal, error] = usePoll<any>(`/research/calendar/${symbol}`, 300000);
  const today = dayOf(cal?.earnings?.as_of);  // the exchange's date, so a replay compares against simulated time
  return (
    <section className="rs-block" aria-labelledby="rs-cal">
      <h3 id="rs-cal">Earnings</h3>
      <SourceLine env={cal?.earnings ?? null} error={error} />
      {cal?.earnings?.available && (
        cal.earnings.items.length ? (
          <ul className="rs-list">
            {cal.earnings.items.slice().reverse().map((e: any) => (
              <li key={`${e.date}-${e.quarter}`} className={e.date >= today ? "rs-upcoming" : ""}>
                <b>{e.date}</b>{e.hour ? <span className="muted"> {e.hour}</span> : null}
                <span>Q{e.quarter} {e.year}</span>
                <span className="muted">EPS est. {money(e.eps_estimate)}{e.eps_actual != null ? ` · actual ${money(e.eps_actual)}` : ""}
                  {e.result ? ` · ${e.result}` : ""}{e.price_reaction_pct != null ? ` · gap ${e.price_reaction_pct > 0 ? "+" : ""}${e.price_reaction_pct}%` : ""}
                  {e.eps_actual == null && e.expected_move_pct != null ? ` · implied move ±${e.expected_move_pct}%` : ""}</span>
              </li>
            ))}
          </ul>
        ) : <p className="muted rs-pad">{cal.earnings.reason || "No earnings dates in the free window (one month back, four months ahead)."}</p>
      )}
      <h3>Dividends and splits</h3>
      <SourceLine env={cal?.dividends ?? null} error={error} />
      {cal?.dividends?.available && (
        cal.dividends.items.length || cal.splits.items.length ? (
          <ul className="rs-list">
            {[...cal.dividends.items, ...cal.splits.items].sort((a: any, b: any) => (b.ex_date || "").localeCompare(a.ex_date || "")).map((r: any) => (
              <li key={r.id} className={(r.ex_date || "") >= today ? "rs-upcoming" : ""}>
                <b>{r.ex_date || r.process_date}</b>
                <span>{r.type === "cash_dividend" ? `Dividend ${money(r.rate, 4)} per share` : `${r.type === "forward_split" ? "Split" : "Reverse split"} ${r.new_rate}-for-${r.old_rate}`}</span>
                <span className="muted">{r.type === "cash_dividend" ? `pays ${r.payable_date || "–"}${r.special ? " · special" : ""}` : "ex-date"}</span>
              </li>
            ))}
          </ul>
        ) : <p className="muted rs-pad">No dividends or splits in the last year or announced.</p>
      )}
    </section>
  );
}

function Fundamentals({ symbol }: { symbol: string }) {
  const [env, error] = usePoll<Envelope>(`/research/fundamentals/${symbol}`, 60000);
  const f = env?.available ? env.data : null;
  const row = (k: string, v: string) => <div key={k}><span>{k}</span><b>{v}</b></div>;
  return (
    <section className="rs-block" aria-labelledby="rs-fund">
      <h3 id="rs-fund">Fundamentals</h3>
      <SourceLine env={env} error={error} />
      {f && (
        <div className="rs-facts">
          {row("Name", f.name || "–")}
          {row("Industry", f.industry || "–")}
          {row("Market cap", compact(f.market_cap))}
          {f.kind !== "fund" && row("P/E (trailing)", f.pe == null ? "–" : f.pe.toFixed(1))}
          {f.kind !== "fund" && row("EPS (trailing)", money(f.eps))}
          {row("Dividend yield", f.dividend_yield_pct == null ? "–" : f.dividend_yield_pct.toFixed(2) + "%")}
          {row("Beta", f.beta == null ? "–" : f.beta.toFixed(2))}
          {row("52-week range", f.low_52w == null ? "–" : `${money(f.low_52w)} – ${money(f.high_52w)}`)}
        </div>
      )}
      {f?.simulated && <><h4 className="rs-sub">{f.kind === "fund" ? "Fund details" : "Key ratios"}</h4><KeyRatios f={f} /></>}
    </section>
  );
}

type Tab = "symbol" | "statements" | "market";

export function Research({ symbol, held = [], onSelect }: { symbol: string; held?: string[]; onSelect?: (s: string) => void }) {
  const [sources] = usePoll<any>("/research/sources", 0);
  const [tab, setTab] = useState<Tab>("symbol");
  if (sources?.reason) {  // the whole panel is unavailable for one reason: say it once
    return <div className="research"><div className="notice rs-notice" role="status">Research data is unavailable. {sources.reason}</div></div>;
  }
  const tabs: [Tab, string][] = [["symbol", symbol], ["statements", "Statements"], ["market", "Market and calendars"]];
  return (
    <div className="research-wrap">
      <div className="rs-seg rs-tabs" role="tablist" aria-label="Research sections">
        {tabs.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={k === tab} className={k === tab ? "on" : ""} onClick={() => setTab(k)}>{label}</button>
        ))}
        {sources?.simulated && <span className="tag" title={sources.note}>simulated research</span>}
      </div>
      {tab === "symbol" && (
        <div className="research">
          <News symbol={symbol} />
          <div className="rs-side">
            <Calendar symbol={symbol} />
            <Fundamentals symbol={symbol} />
          </div>
        </div>
      )}
      {tab === "statements" && <div className="research rs-one"><Statements symbol={symbol} /></div>}
      {tab === "market" && (
        <div className="research">
          <MarketNews />
          <div className="rs-side">
            <EarningsCalendar held={held} onSelect={onSelect} />
            <EconomicCalendar />
          </div>
        </div>
      )}
    </div>
  );
}

/** Banner suffix naming the market data source, used by the paper-trading banner in App.tsx. */
export function feedBanner(s: any): { text: string; title: string } {
  if (!s) return { text: "", title: "" };
  const title = [s.data_note, s.fallback_reason, s.stream?.message].filter(Boolean).join(" ");
  if (s.simulated_data) return { text: " · simulated market data", title };
  const src = s.data_source || s.feed;
  if (s.stale) return { text: ` · ${src} prices may be stale (feed interrupted)`, title };
  if (s.realtime === false || s.delay_seconds) return { text: ` · ${src} data delayed ${Math.round((s.delay_seconds || 900) / 60)} min`, title };
  return { text: ` · real-time ${s.data_exchange || ""} prices via ${src}`.replace("  ", " "), title };
}
